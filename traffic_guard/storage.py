"""计数存储后端抽象与内存实现。

存储后端对上层暴露两类能力：
- apply(key, fn): 对单个 key 的原子"读-改-写"，fn 在 key 级锁内执行，
  保证并发请求之间计数一致、不会出现额度穿透。
- get/set/delete: 简单键值操作，支持可选 TTL（用于封禁记录与状态清理）。

后端实现不可用时必须抛出 StorageError，由上层按降级策略处理，
不得把后端异常直接抛给请求方。
"""

from __future__ import annotations

import threading
import time
from abc import ABC, abstractmethod
from typing import Any, Callable, TypeVar

T = TypeVar("T")


class StorageError(Exception):
    """存储后端不可用或操作失败。"""


class StorageBackend(ABC):
    """限流计数存储后端接口。"""

    @abstractmethod
    def apply(self, key: str, fn: Callable[[Any], tuple[Any, T]]) -> T:
        """对 key 对应的状态执行原子读-改-写。

        fn 接收当前状态（不存在时为 None），返回 (新状态, 结果)。
        新状态会替换旧状态写回存储；返回 fn 的结果。
        """

    @abstractmethod
    def get(self, key: str) -> Any:
        """读取 key 的值，不存在或已过期返回 None。"""

    @abstractmethod
    def set(self, key: str, value: Any, ttl: float | None = None) -> None:
        """写入 key 的值，可选 TTL（秒），过期后自动失效。"""

    @abstractmethod
    def delete(self, key: str) -> None:
        """删除 key。"""


class InMemoryBackend(StorageBackend):
    """基于分条锁（striped lock）的进程内存储。

    使用固定数量的锁条带而非全局锁，不同 key 的计数互不阻塞，
    避免计数过程在高并发下成为明显瓶颈。
    """

    def __init__(self, stripes: int = 64, clock: Callable[[], float] = time.monotonic):
        self._store: dict[str, tuple[Any, float | None]] = {}
        self._locks = [threading.Lock() for _ in range(stripes)]
        self._clock = clock

    def _lock_for(self, key: str) -> threading.Lock:
        return self._locks[hash(key) % len(self._locks)]

    def _is_expired(self, expire_at: float | None) -> bool:
        return expire_at is not None and self._clock() >= expire_at

    def apply(self, key: str, fn: Callable[[Any], tuple[Any, T]]) -> T:
        lock = self._lock_for(key)
        with lock:
            entry = self._store.get(key)
            current = None
            if entry is not None:
                value, expire_at = entry
                if self._is_expired(expire_at):
                    self._store.pop(key, None)
                else:
                    current = value
            new_value, result = fn(current)
            self._store[key] = (new_value, None)
            return result

    def get(self, key: str) -> Any:
        lock = self._lock_for(key)
        with lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            value, expire_at = entry
            if self._is_expired(expire_at):
                self._store.pop(key, None)
                return None
            return value

    def set(self, key: str, value: Any, ttl: float | None = None) -> None:
        expire_at = None if ttl is None else self._clock() + ttl
        lock = self._lock_for(key)
        with lock:
            self._store[key] = (value, expire_at)

    def delete(self, key: str) -> None:
        lock = self._lock_for(key)
        with lock:
            self._store.pop(key, None)
