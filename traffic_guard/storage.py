"""Storage backends for rate-limit counters.

The limiter only talks to the Storage protocol, so backends can be swapped
(in-memory, Redis, failing wrappers for degradation tests).

Concurrency contract: every operation is atomic with respect to other
operations on the same key. MemoryStorage uses per-key locks; RedisStorage
uses Lua scripts.
"""

from __future__ import annotations

import threading
import time

from .clock import SystemClock
from dataclasses import dataclass
from typing import Protocol


@dataclass
class SlidingRecord:
    count: int
    oldest: float | None  # oldest timestamp still inside the window


class StorageError(RuntimeError):
    """Raised when a backend operation fails."""


class Storage(Protocol):
    def ping(self) -> None:
        """Raise StorageError if the backend is unreachable."""
        ...

    def fixed_incr(self, key: str, ttl_seconds: float) -> int:
        """Atomically increment a counter; create it with the given TTL if absent."""
        ...

    def sliding_record(self, key: str, now: float, window_seconds: float) -> SlidingRecord:
        """Atomically drop timestamps outside the window, append ``now`` and count."""
        ...

    def get_int(self, key: str) -> int | None: ...

    def delete(self, key: str) -> None: ...


class _MemoryBackend:
    """Shared in-memory state with per-key locks."""

    def __init__(self, max_keys: int) -> None:
        self._max_keys = max_keys
        self._meta_lock = threading.Lock()
        self._locks: dict[str, threading.RLock] = {}
        self._fixed: dict[str, tuple[int, float]] = {}  # key -> (count, expires_at)
        self._sliding: dict[str, list[float]] = {}  # key -> timestamps
        self._sliding_expiry: dict[str, float] = {}  # key -> expires_at (now + window)

    def lock_for(self, key: str) -> threading.RLock:
        with self._meta_lock:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.RLock()
                self._locks[key] = lock
            return lock

    def maybe_evict(self, now: float) -> None:
        if len(self._fixed) + len(self._sliding) <= self._max_keys:
            return
        with self._meta_lock:
            expired_fixed = [k for k, (_, exp) in self._fixed.items() if exp <= now]
            for k in expired_fixed:
                self._fixed.pop(k, None)
                self._locks.pop(k, None)
            expired_sliding = [
                k for k, exp in self._sliding_expiry.items() if exp <= now
            ]
            for k in expired_sliding:
                self._sliding.pop(k, None)
                self._sliding_expiry.pop(k, None)
                self._locks.pop(k, None)


class MemoryStorage:
    """Thread-safe in-process storage. Used by default and as local fallback."""

    def __init__(self, max_keys: int = 100000, clock=None) -> None:
        self._backend = _MemoryBackend(max_keys)
        self._clock = clock or SystemClock()

    def ping(self) -> None:
        return None

    def fixed_incr(self, key: str, ttl_seconds: float) -> int:
        now = self._clock.now()
        self._backend.maybe_evict(now)
        lock = self._backend.lock_for(key)
        with lock:
            count, expires_at = self._backend._fixed.get(key, (0, 0.0))
            if expires_at <= now:
                count = 0
                expires_at = now + ttl_seconds
            count += 1
            self._backend._fixed[key] = (count, expires_at)
            return count

    def sliding_record(self, key: str, now: float, window_seconds: float) -> SlidingRecord:
        self._backend.maybe_evict(now)
        lock = self._backend.lock_for(key)
        with lock:
            timestamps = self._backend._sliding.get(key)
            if timestamps is None:
                timestamps = []
                self._backend._sliding[key] = timestamps
            cutoff = now - window_seconds
            # Drop timestamps outside the window (list stays sorted).
            while timestamps and timestamps[0] <= cutoff:
                timestamps.pop(0)
            timestamps.append(now)
            self._backend._sliding_expiry[key] = now + window_seconds
            oldest = timestamps[0] if timestamps else None
            return SlidingRecord(count=len(timestamps), oldest=oldest)

    def get_int(self, key: str) -> int | None:
        with self._backend.lock_for(key):
            entry = self._backend._fixed.get(key)
            if entry is None:
                return None
            count, expires_at = entry
            if expires_at <= self._clock.now():
                return None
            return count

    def delete(self, key: str) -> None:
        with self._backend.lock_for(key):
            self._backend._fixed.pop(key, None)
            self._backend._sliding.pop(key, None)
            self._backend._sliding_expiry.pop(key, None)


class FailingStorage:
    """Test double: wraps another storage and fails after ``fail_after`` calls.

    ``fail_after=None`` fails immediately; ``fail_after=0`` also fails
    immediately; ``fail_after=N`` lets the first N operations succeed.
    """

    def __init__(self, delegate: Storage, fail_after: int | None = None) -> None:
        self._delegate = delegate
        self._fail_after = fail_after
        self._calls = 0
        self._lock = threading.Lock()

    def _before(self) -> None:
        with self._lock:
            if self._fail_after is None or self._calls >= self._fail_after:
                raise StorageError("simulated storage backend failure")
            self._calls += 1

    def ping(self) -> None:
        with self._lock:
            if self._fail_after is None or self._calls >= self._fail_after:
                raise StorageError("simulated storage backend failure")
            self._calls += 1

    def fixed_incr(self, key: str, ttl_seconds: float) -> int:
        self._before()
        return self._delegate.fixed_incr(key, ttl_seconds)

    def sliding_record(self, key: str, now: float, window_seconds: float) -> SlidingRecord:
        self._before()
        return self._delegate.sliding_record(key, now, window_seconds)

    def get_int(self, key: str) -> int | None:
        self._before()
        return self._delegate.get_int(key)

    def delete(self, key: str) -> None:
        self._before()
        self._delegate.delete(key)


# Lua scripts for atomic Redis operations.
_FIXED_INCR_LUA = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then
  redis.call('PEXPIRE', KEYS[1], ARGV[1])
end
return count
"""

_SLIDING_RECORD_LUA = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[1])
redis.call('ZADD', KEYS[1], ARGV[2], ARGV[3])
redis.call('PEXPIRE', KEYS[1], ARGV[4])
local count = redis.call('ZCARD', KEYS[1])
local oldest = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
if #oldest == 0 then
  return {count, false}
end
return {count, oldest[2]}
"""


class RedisStorage:
    """Redis-backed storage using Lua scripts for atomicity.

    Requires the optional ``redis`` package. Not installed in this environment;
    import it lazily so the rest of the package works without it.
    """

    def __init__(self, url: str = "redis://localhost:6379/0", prefix: str = "tg:") -> None:
        try:
            import redis  # type: ignore[import-not-found]
        except ImportError as exc:
            raise StorageError(
                "redis package is required for RedisStorage; install with `pip install redis`"
            ) from exc
        self._redis = redis.from_url(url, decode_responses=True)
        self._prefix = prefix

    def _key(self, key: str) -> str:
        return f"{self._prefix}{key}"

    def ping(self) -> None:
        try:
            self._redis.ping()
        except Exception as exc:  # noqa: BLE001
            raise StorageError(f"redis ping failed: {exc}") from exc

    def fixed_incr(self, key: str, ttl_seconds: float) -> int:
        try:
            return int(
                self._redis.eval(
                    _FIXED_INCR_LUA, 1, self._key(key), int(ttl_seconds * 1000)
                )
            )
        except StorageError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise StorageError(f"redis fixed_incr failed: {exc}") from exc

    def sliding_record(self, key: str, now: float, window_seconds: float) -> SlidingRecord:
        try:
            raw = self._redis.eval(
                _SLIDING_RECORD_LUA,
                1,
                self._key(key),
                now - window_seconds,
                now,
                f"{now:.6f}",
                int(window_seconds * 1000),
            )
        except StorageError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise StorageError(f"redis sliding_record failed: {exc}") from exc
        count = int(raw[0])
        oldest = float(raw[1]) if raw[1] is not False else None
        return SlidingRecord(count=count, oldest=oldest)

    def get_int(self, key: str) -> int | None:
        try:
            value = self._redis.get(self._key(key))
        except Exception as exc:  # noqa: BLE001
            raise StorageError(f"redis get failed: {exc}") from exc
        return int(value) if value is not None else None

    def delete(self, key: str) -> None:
        try:
            self._redis.delete(self._key(key))
        except Exception as exc:  # noqa: BLE001
            raise StorageError(f"redis delete failed: {exc}") from exc
