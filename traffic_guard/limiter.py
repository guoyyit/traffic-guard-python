"""TrafficGuard: combines rules, strategies, bans and degradation into one check."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .ban import BanManager
from .clock import Clock, SystemClock
from .config import ConfigLoader, LimitRule, TrafficGuardConfig
from .metrics import Metrics
from .storage import MemoryStorage, Storage, StorageError
from .strategies import Decision, FixedWindowStrategy, SlidingWindowStrategy

logger = logging.getLogger("traffic_guard")


@dataclass
class CheckResult:
    allowed: bool
    status_code: int  # 200 allowed, 429 rate limited, 403 banned, 503 backend down (fail-close)
    message: str
    retry_after: float | None
    limit: int | None = None
    remaining: int | None = None
    reset_at: float | None = None
    degraded: bool = False
    rule: LimitRule | None = None


class TrafficGuard:
    """Two-dimensional (source x interface) rate limiter with abuse protection."""

    def __init__(
        self,
        config_loader: ConfigLoader | None = None,
        storage: Storage | None = None,
        clock: Clock | None = None,
        metrics: Metrics | None = None,
    ) -> None:
        self._config_loader = config_loader or ConfigLoader()
        self._storage = storage if storage is not None else MemoryStorage()
        self._clock = clock if clock is not None else SystemClock()
        self._metrics = metrics if metrics is not None else Metrics()
        self._ban_manager: BanManager | None = None
        self._ban_key: tuple | None = None
        self._local_storage: MemoryStorage | None = None

    @property
    def metrics(self) -> Metrics:
        return self._metrics

    @property
    def ban_manager(self) -> BanManager:
        # Ban config may change on hot reload; rebuild the manager when it does,
        # preserving active bans so a reload never lifts an in-progress ban.
        config = self._config_loader.get()
        key = self._ban_manager_config_key(config)
        if self._ban_manager is None or key != self._ban_key:
            previous = self._ban_manager
            self._ban_manager = BanManager(config.ban, self._clock, self._metrics)
            if previous is not None:
                self._ban_manager.transfer_state_from(previous)
            self._ban_key = key
        return self._ban_manager

    @staticmethod
    def _ban_manager_config_key(config: TrafficGuardConfig) -> tuple:
        b = config.ban
        return (b.enabled, b.threshold, b.violation_window_seconds, b.ban_seconds)

    def check(self, source: str, interface: str) -> CheckResult:
        config = self._config_loader.get()

        # 1. Whitelist / exemption rules skip all limiting.
        if source in config.whitelist_sources or interface in config.whitelist_interfaces:
            self._metrics.incr("whitelisted_total")
            return CheckResult(
                allowed=True,
                status_code=200,
                message="allowed (whitelisted)",
                retry_after=None,
            )

        # 2. Active bans are enforced before any storage call.
        ban_status = self.ban_manager.is_banned(source)
        if ban_status.banned:
            self._metrics.incr("banned_total")
            return CheckResult(
                allowed=False,
                status_code=403,
                message="temporarily banned due to repeated rate-limit violations",
                retry_after=ban_status.retry_after,
            )

        # 3. Pick the rule and strategy for this interface.
        rule = config.rule_for(interface)
        key = self._limit_key(source, interface)
        decision, degraded, status_override = self._check_with_degradation(key, rule, config)

        # 4. Verdict.
        if decision.allowed:
            self._metrics.incr("allowed_total")
            return CheckResult(
                allowed=True,
                status_code=200,
                message="allowed",
                retry_after=None,
                limit=decision.limit,
                remaining=decision.remaining,
                reset_at=decision.reset_at,
                degraded=degraded,
                rule=rule,
            )

        if status_override is not None:
            # Backend unavailable with fail-close: deny without recording a
            # rate-limit violation (the client is not at fault).
            return CheckResult(
                allowed=False,
                status_code=status_override,
                message="rate limiter unavailable",
                retry_after=decision.retry_after,
                limit=decision.limit,
                remaining=decision.remaining,
                reset_at=decision.reset_at,
                degraded=degraded,
                rule=rule,
            )

        self._metrics.incr("limited_total")
        self.ban_manager.record_violation(source)
        return CheckResult(
            allowed=False,
            status_code=429,
            message="rate limit exceeded",
            retry_after=decision.retry_after,
            limit=decision.limit,
            remaining=decision.remaining,
            reset_at=decision.reset_at,
            degraded=degraded,
            rule=rule,
        )

    def _check_with_degradation(
        self, key: str, rule: LimitRule, config: TrafficGuardConfig
    ) -> tuple[Decision, bool, int | None]:
        """Run the strategy, applying the configured degradation on backend failure.

        Returns (decision, degraded, status_override). ``status_override`` is set
        to 503 for fail-close so the verdict does not get mapped to 429.
        """
        try:
            return self._run_strategy(self._storage, key, rule), False, None
        except StorageError as exc:
            self._metrics.incr("backend_errors_total")
            mode = config.degradation.on_backend_failure
            logger.error("traffic guard backend error: %s (degradation=%s)", exc, mode)
            if mode == "fail_open":
                return self._allow_degraded(rule), True, None
            if mode == "fail_close":
                return self._deny_degraded(rule), True, 503
            # local_fallback: retry against local in-process storage.
            try:
                if self._local_storage is None:
                    self._local_storage = MemoryStorage(
                        max_keys=config.degradation.local_fallback_max_keys
                    )
                return self._run_strategy(self._local_storage, key, rule), True, None
            except StorageError as local_exc:
                # Local storage cannot realistically fail; fail open to stay available.
                logger.error("local fallback also failed: %s; failing open", local_exc)
                return self._allow_degraded(rule), True, None

    def _run_strategy(self, storage: Storage, key: str, rule: LimitRule) -> Decision:
        if rule.strategy.value == "sliding_window":
            return SlidingWindowStrategy(storage, self._clock).check(key, rule)
        return FixedWindowStrategy(storage, self._clock).check(key, rule)

    @staticmethod
    def _allow_degraded(rule: LimitRule) -> Decision:
        return Decision(
            allowed=True, limit=rule.requests, remaining=rule.requests,
            retry_after=None, reset_at=0.0,
        )

    @staticmethod
    def _deny_degraded(rule: LimitRule) -> Decision:
        return Decision(
            allowed=False, limit=rule.requests, remaining=0,
            retry_after=rule.window_seconds, reset_at=0.0,
        )

    @staticmethod
    def _limit_key(source: str, interface: str) -> str:
        return f"limit:{source}:{interface}"
