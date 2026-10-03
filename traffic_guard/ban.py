"""违规来源的临时封禁与自动解封。

语义：
- 来源在 violation_window_seconds 内累计 violation_threshold 次限流违规即被封禁，
  封禁时长 ban_seconds，到期自动解封（惰性：下次访问时发现已过期即解除）。
- 封禁期间的请求在进入计数逻辑前被直接拒绝，不消耗后端计数资源。
- 封禁与解封均通过 logger 输出事件，便于观测与告警。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable

from traffic_guard.config import BanConfig
from traffic_guard.storage import StorageBackend

logger = logging.getLogger("traffic_guard.ban")

_BAN_KEY_PREFIX = "traffic_guard:ban:"
_VIOLATION_KEY_PREFIX = "traffic_guard:violations:"


@dataclass(frozen=True)
class BanStatus:
    """来源当前的封禁状态。"""

    banned: bool
    remaining_seconds: float = 0.0


class BanManager:
    """基于存储后端的封禁管理器。"""

    def __init__(
        self,
        storage: StorageBackend,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._storage = storage
        self._clock = clock

    def check(self, identity: str) -> BanStatus:
        """检查来源是否处于封禁期；发现已自然过期的记录时执行惰性解封。"""
        key = _BAN_KEY_PREFIX + identity
        ban_until = self._storage.get(key)
        if ban_until is None:
            return BanStatus(banned=False)
        now = self._clock()
        if now < ban_until:
            return BanStatus(banned=True, remaining_seconds=ban_until - now)
        # 封禁已到期：自动解封并记录事件
        self._storage.delete(key)
        logger.info("identity %r auto-unbanned (ban expired)", identity)
        return BanStatus(banned=False)

    def record_violation(self, identity: str, config: BanConfig) -> BanStatus:
        """记录一次限流违规，达到阈值时实施封禁。

        违规计数采用滑动窗口：仅统计最近 violation_window_seconds 内的违规。
        返回记录违规后的封禁状态。
        """
        if not config.enabled:
            return BanStatus(banned=False)
        now = self._clock()
        cutoff = now - config.violation_window_seconds
        violation_key = _VIOLATION_KEY_PREFIX + identity

        def count_violation(current):
            timestamps = [ts for ts in (current or []) if ts > cutoff]
            timestamps.append(now)
            return timestamps, len(timestamps)

        violations = self._storage.apply(violation_key, count_violation)
        if violations < config.violation_threshold:
            return BanStatus(banned=False)

        # 不设置存储层 TTL：到期判定与惰性解封统一由 check() 完成，
        # 保证自动解封事件可观测
        ban_until = now + config.ban_seconds
        self._storage.set(_BAN_KEY_PREFIX + identity, ban_until)
        # 封禁后清空违规计数，避免解封后立即因历史违规再次被封
        self._storage.delete(violation_key)
        logger.warning(
            "identity %r banned for %.1fs after %d violations",
            identity,
            config.ban_seconds,
            violations,
        )
        return BanStatus(banned=True, remaining_seconds=config.ban_seconds)

    def unban(self, identity: str) -> None:
        """手动解除封禁（管理操作）。"""
        self._storage.delete(_BAN_KEY_PREFIX + identity)
        logger.info("identity %r manually unbanned", identity)
