"""Temporary banning of sources that repeatedly violate rate limits.

Ban state lives in-process on purpose: banned requests are rejected before any
storage call, so a ban keeps working while the shared backend is down and never
consumes backend quota. Bans expire automatically; both ban and unban are
logged and counted in metrics so the lifecycle is observable.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from dataclasses import dataclass

from .clock import Clock
from .config import BanConfig

logger = logging.getLogger("traffic_guard")


@dataclass
class BanStatus:
    banned: bool
    retry_after: float | None = None


@dataclass
class BanEvent:
    source: str
    banned_at: float
    until: float
    reason: str


@dataclass
class UnbanEvent:
    source: str
    unbanned_at: float
    reason: str  # "expired"


class BanManager:
    """Tracks violation counts and temporary bans in-process (thread-safe)."""

    def __init__(self, config: BanConfig, clock: Clock, metrics=None) -> None:
        self._config = config
        self._clock = clock
        self._metrics = metrics
        self._lock = threading.Lock()
        self._violations: dict[str, list[float]] = {}
        self._bans: dict[str, float] = {}  # source -> banned until epoch
        self._events: deque[BanEvent | UnbanEvent] = deque(maxlen=200)

    def is_banned(self, source: str) -> BanStatus:
        now = self._clock.now()
        with self._lock:
            until = self._bans.get(source)
            if until is None:
                return BanStatus(banned=False)
            if until <= now:
                self._bans.pop(source, None)
                self._violations.pop(source, None)
                event = UnbanEvent(source=source, unbanned_at=now, reason="expired")
                self._events.append(event)
                logger.info("traffic guard: source %s unbanned (expired)", source)
                if self._metrics is not None:
                    self._metrics.incr("bans_unbanned_total")
                return BanStatus(banned=False)
            return BanStatus(banned=True, retry_after=until - now)

    def record_violation(self, source: str) -> BanEvent | None:
        """Record a rate-limit violation; return a BanEvent if a new ban started."""
        if not self._config.enabled:
            return None
        now = self._clock.now()
        with self._lock:
            if source in self._bans:
                return None  # already banned; ban is enforced before this is called
            violations = self._violations.setdefault(source, [])
            cutoff = now - self._config.violation_window_seconds
            while violations and violations[0] <= cutoff:
                violations.pop(0)
            violations.append(now)
            if len(violations) < self._config.threshold:
                return None
            until = now + self._config.ban_seconds
            self._bans[source] = until
            self._violations.pop(source, None)
            event = BanEvent(
                source=source,
                banned_at=now,
                until=until,
                reason=f"{len(violations)} violations in {self._config.violation_window_seconds:.0f}s",
            )
            self._events.append(event)
            logger.warning(
                "traffic guard: source %s banned until %.1f (%s)",
                source,
                until,
                event.reason,
            )
            if self._metrics is not None:
                self._metrics.incr("bans_total")
            return event

    def list_bans(self) -> list[BanEvent]:
        """Return currently active bans (reconstructed from internal state)."""
        now = self._clock.now()
        with self._lock:
            active = []
            for source, until in list(self._bans.items()):
                if until > now:
                    active.append(
                        BanEvent(
                            source=source,
                            banned_at=until - self._config.ban_seconds,
                            until=until,
                            reason="rate limit violations",
                        )
                    )
            return active

    def recent_events(self) -> list[BanEvent | UnbanEvent]:
        with self._lock:
            return list(self._events)

    def transfer_state_from(self, previous: "BanManager") -> None:
        """Preserve active bans and violation history across a config reload."""
        with previous._lock:
            bans = dict(previous._bans)
            violations = {k: list(v) for k, v in previous._violations.items()}
            events = list(previous._events)
        with self._lock:
            self._bans = bans
            self._violations = violations
            self._events.extend(events)
