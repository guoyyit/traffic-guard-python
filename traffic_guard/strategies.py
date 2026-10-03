"""Rate-limit strategies.

Window boundary semantics (see README for details):

* fixed_window: time is sliced into aligned windows of ``window_seconds`` starting
  at the Unix epoch. A counter is shared by all requests in one slice and resets
  atomically when the next slice begins.
* sliding_window: a log of request timestamps is kept per key; only timestamps
  within the last ``window_seconds`` count. The window slides with time, so there
  is no fixed alignment boundary.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .clock import Clock
from .config import LimitRule
from .storage import Storage


@dataclass
class Decision:
    allowed: bool
    limit: int
    remaining: int
    retry_after: float | None  # seconds until a slot becomes available
    reset_at: float  # epoch seconds at which the current window fully resets


class FixedWindowStrategy:
    """Fixed counter windows aligned to the Unix epoch."""

    name = "fixed_window"

    def __init__(self, storage: Storage, clock: Clock) -> None:
        self._storage = storage
        self._clock = clock

    def check(self, key: str, rule: LimitRule) -> Decision:
        now = self._clock.now()
        window_index = math.floor(now / rule.window_seconds)
        window_start = window_index * rule.window_seconds
        window_key = f"{key}:fw:{window_index}"
        count = self._storage.fixed_incr(window_key, ttl_seconds=rule.window_seconds)
        allowed = count <= rule.requests
        remaining = max(0, rule.requests - count)
        reset_at = window_start + rule.window_seconds
        retry_after = None if allowed else max(0.0, reset_at - now)
        return Decision(
            allowed=allowed,
            limit=rule.requests,
            remaining=remaining,
            retry_after=retry_after,
            reset_at=reset_at,
        )


class SlidingWindowStrategy:
    """Sliding window log: only requests from the last ``window_seconds`` count."""

    name = "sliding_window"

    def __init__(self, storage: Storage, clock: Clock) -> None:
        self._storage = storage
        self._clock = clock

    def check(self, key: str, rule: LimitRule) -> Decision:
        now = self._clock.now()
        record = self._storage.sliding_record(
            f"{key}:sw", now=now, window_seconds=rule.window_seconds
        )
        allowed = record.count <= rule.requests
        remaining = max(0, rule.requests - record.count)
        reset_at = now + rule.window_seconds
        if allowed or record.oldest is None:
            retry_after = None
        else:
            # Wait until the oldest recorded request leaves the window.
            retry_after = max(0.0, record.oldest + rule.window_seconds - now)
        return Decision(
            allowed=allowed,
            limit=rule.requests,
            remaining=remaining,
            retry_after=retry_after,
            reset_at=reset_at,
        )
