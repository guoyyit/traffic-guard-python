"""Clock abstraction so time-dependent logic is deterministic in tests."""

from __future__ import annotations

import time
from typing import Protocol


class Clock(Protocol):
    def now(self) -> float: ...


class SystemClock:
    """Wall-clock time in seconds (Unix epoch)."""

    def now(self) -> float:
        return time.time()


class FakeClock:
    """Controllable clock for deterministic window/boundary tests."""

    def __init__(self, start: float = 0.0) -> None:
        self._now = float(start)

    def now(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("cannot move clock backwards")
        self._now += float(seconds)
