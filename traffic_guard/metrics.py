"""In-process observability counters (thread-safe)."""

from __future__ import annotations

import threading


class Metrics:
    """Simple monotonic counters. All operations are thread-safe."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, int] = {}

    def incr(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self._counters[name] = self._counters.get(name, 0) + amount

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counters)
