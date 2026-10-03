"""限流核心编排：白名单、封禁、策略判定、降级。

一次请求的判定流程（任一环节命中即短路）：
1. 白名单：来源或接口在豁免名单中，直接放行；
2. 封禁：来源处于封禁期，直接拒绝（不消耗计数资源）；
3. 限流：按命中的规则选择策略，在存储后端原子计数后判定；
4. 违规记录：被限流时记录违规，达到阈值触发临时封禁；
5. 降级：存储后端抛 StorageError 时按 degrade_mode 放行或拒绝，
   后端异常绝不向请求方传播。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable

from traffic_guard.ban import BanManager
from traffic_guard.config import DegradeMode, TrafficGuardConfig
from traffic_guard.storage import StorageBackend, StorageError
from traffic_guard.strategies import get_strategy_fn

logger = logging.getLogger("traffic_guard.limiter")

#: 降级拒绝时建议的客户端退避秒数（存储不可用，无法计算真实窗口）
DEGRADED_RETRY_AFTER = 1.0

_COUNTER_KEY_PREFIX = "traffic_guard:counter:"


@dataclass(frozen=True)
class LimitResult:
    """单次请求的限流判定结果。"""

    allowed: bool
    limit: int = 0
    remaining: int = 0
    retry_after: float = 0.0
    reason: str = "ok"  # ok | whitelisted | rate_limited | banned | degraded_open | degraded_closed


class RateLimiter:
    """按 (来源身份, 接口) 两个维度进行限流的判定入口。

    config_provider 每次判定时取最新配置，因此规则、白名单、阈值
    的调整无需重启服务即可生效（配合 ConfigReloader 使用）。
    """

    def __init__(
        self,
        storage: StorageBackend,
        config_provider: Callable[[], TrafficGuardConfig],
        clock: Callable[[], float] = time.monotonic,
    ):
        self._storage = storage
        self._config_provider = config_provider
        self._clock = clock
        self._ban_manager = BanManager(storage, clock=clock)

    @property
    def ban_manager(self) -> BanManager:
        return self._ban_manager

    def check(self, identity: str, endpoint: str) -> LimitResult:
        config = self._config_provider()

        # 1. 白名单豁免
        if config.is_whitelisted(identity, endpoint):
            return LimitResult(allowed=True, reason="whitelisted")

        # 2. 封禁检查（封禁中的请求直接拒绝，不进入计数）
        try:
            ban_status = self._ban_manager.check(identity)
        except StorageError:
            return self._degrade(config, identity, endpoint)
        if ban_status.banned:
            return LimitResult(
                allowed=False,
                retry_after=ban_status.remaining_seconds,
                reason="banned",
            )

        # 3. 策略判定（原子计数）
        rule = config.find_rule(identity, endpoint)
        strategy_fn = get_strategy_fn(rule.strategy)
        key = f"{_COUNTER_KEY_PREFIX}{identity}:{endpoint}"
        now = self._clock()

        def hit(current):
            return strategy_fn(current, now, rule.limit, rule.window_seconds)

        try:
            decision = self._storage.apply(key, hit)
        except StorageError:
            return self._degrade(config, identity, endpoint)

        if decision.allowed:
            return LimitResult(
                allowed=True,
                limit=decision.limit,
                remaining=decision.remaining,
                reason="ok",
            )

        # 4. 记录违规，可能触发封禁
        try:
            ban_status = self._ban_manager.record_violation(identity, config.ban)
        except StorageError:
            ban_status = None
        retry_after = decision.retry_after
        reason = "rate_limited"
        if ban_status is not None and ban_status.banned:
            retry_after = max(retry_after, ban_status.remaining_seconds)
            reason = "banned"
        return LimitResult(
            allowed=False,
            limit=decision.limit,
            remaining=0,
            retry_after=retry_after,
            reason=reason,
        )

    def _degrade(
        self, config: TrafficGuardConfig, identity: str, endpoint: str
    ) -> LimitResult:
        if config.degrade_mode is DegradeMode.FAIL_OPEN:
            logger.warning(
                "storage unavailable, fail-open: allowing %r -> %r",
                identity,
                endpoint,
            )
            return LimitResult(allowed=True, reason="degraded_open")
        logger.warning(
            "storage unavailable, fail-closed: rejecting %r -> %r",
            identity,
            endpoint,
        )
        return LimitResult(
            allowed=False,
            retry_after=DEGRADED_RETRY_AFTER,
            reason="degraded_closed",
        )
