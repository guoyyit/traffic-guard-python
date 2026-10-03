"""限流策略：固定窗口与滑动窗口。

策略实现为纯函数：输入当前状态与判定时刻，输出判定结果与新状态，
不直接访问时钟或存储，便于对窗口边界做确定性测试。

窗口边界约定（可预期、可复现）：
- 固定窗口：窗口起点为 floor(now / window) * window，即按自然时间对齐的
  固定区间 [k*window, (k+1)*window)，now 恰好等于右边界时计入下一个窗口。
- 滑动窗口：统计区间 (now - window, now]，即严格早于 now - window 的记录
  被剔除，等于边界的记录仍计入当前窗口。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from traffic_guard.config import StrategyType


@dataclass(frozen=True)
class Decision:
    """单次限流判定结果。"""

    allowed: bool
    limit: int
    remaining: int
    retry_after: float  # 被限流时建议客户端等待的秒数；放行时为 0.0


def fixed_window_hit(
    state: Any, now: float, limit: int, window_seconds: float
) -> tuple[Any, Decision]:
    """固定窗口计数。

    state 为 (window_start, count) 或 None。窗口按自然时间对齐，
    进入新窗口时计数清零。
    """
    window_start = math.floor(now / window_seconds) * window_seconds
    if state is None or state[0] != window_start:
        count = 0
    else:
        count = state[1]
    count += 1
    new_state = (window_start, count)
    reset_at = window_start + window_seconds
    if count <= limit:
        return new_state, Decision(
            allowed=True,
            limit=limit,
            remaining=limit - count,
            retry_after=0.0,
        )
    return new_state, Decision(
        allowed=False,
        limit=limit,
        remaining=0,
        retry_after=max(reset_at - now, 0.0),
    )


def sliding_window_hit(
    state: Any, now: float, limit: int, window_seconds: float
) -> tuple[Any, Decision]:
    """滑动窗口日志。

    state 为请求时间戳列表或 None。仅保留 (now - window, now] 内的记录，
    超过限额时以最早记录的过期时间计算 retry_after。
    """
    cutoff = now - window_seconds
    timestamps = [ts for ts in (state or []) if ts > cutoff]
    if len(timestamps) < limit:
        timestamps.append(now)
        return timestamps, Decision(
            allowed=True,
            limit=limit,
            remaining=limit - len(timestamps),
            retry_after=0.0,
        )
    retry_after = max(timestamps[0] + window_seconds - now, 0.0)
    return timestamps, Decision(
        allowed=False,
        limit=limit,
        remaining=0,
        retry_after=retry_after,
    )


_STRATEGIES = {
    StrategyType.FIXED_WINDOW: fixed_window_hit,
    StrategyType.SLIDING_WINDOW: sliding_window_hit,
}


def get_strategy_fn(strategy: StrategyType):
    """按配置的策略类型返回对应判定函数。"""
    return _STRATEGIES[strategy]
