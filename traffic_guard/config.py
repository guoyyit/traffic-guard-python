"""限流配置定义与热加载。

配置语义：
- 限流维度为 (来源身份 identity, 接口 endpoint) 二元组，规则按 identity / endpoint
  的匹配模式命中，未命中任何规则时使用 default_rule。
- 白名单（whitelisted_identities / whitelisted_endpoints）中的来源或接口完全跳过限流。
- 配置可通过 JSON 文件提供，ConfigReloader 基于文件 mtime 实现不重启的热更新。
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class StrategyType(str, Enum):
    """限流策略类型。"""

    FIXED_WINDOW = "fixed_window"
    SLIDING_WINDOW = "sliding_window"


class DegradeMode(str, Enum):
    """存储后端不可用时的降级策略。

    FAIL_OPEN: 放行请求（优先可用性）。
    FAIL_CLOSED: 拒绝请求并返回限流响应（优先保护后端）。
    """

    FAIL_OPEN = "fail_open"
    FAIL_CLOSED = "fail_closed"


@dataclass(frozen=True)
class RateLimitRule:
    """单条限流规则。

    identity / endpoint 支持精确匹配或 "*" 通配（匹配任意值）。
    limit 为窗口内允许的最大请求数；window_seconds 为窗口长度（秒）。
    """

    name: str
    identity: str = "*"
    endpoint: str = "*"
    strategy: StrategyType = StrategyType.FIXED_WINDOW
    limit: int = 100
    window_seconds: float = 60.0

    def matches(self, identity: str, endpoint: str) -> bool:
        return (self.identity in ("*", identity)) and (
            self.endpoint in ("*", endpoint)
        )


@dataclass(frozen=True)
class BanConfig:
    """临时封禁配置。

    violation_threshold 次限流违规（在 violation_window_seconds 内）触发封禁，
    封禁时长为 ban_seconds，到期自动解封。
    """

    enabled: bool = True
    violation_threshold: int = 5
    violation_window_seconds: float = 60.0
    ban_seconds: float = 300.0


@dataclass(frozen=True)
class TrafficGuardConfig:
    """限流组件整体配置。"""

    rules: tuple[RateLimitRule, ...] = ()
    default_rule: RateLimitRule = field(
        default_factory=lambda: RateLimitRule(name="default")
    )
    ban: BanConfig = field(default_factory=BanConfig)
    degrade_mode: DegradeMode = DegradeMode.FAIL_OPEN
    whitelisted_identities: frozenset[str] = frozenset()
    whitelisted_endpoints: frozenset[str] = frozenset()

    def find_rule(self, identity: str, endpoint: str) -> RateLimitRule:
        """按声明顺序返回第一条命中的规则，否则返回默认规则。"""
        for rule in self.rules:
            if rule.matches(identity, endpoint):
                return rule
        return self.default_rule

    def is_whitelisted(self, identity: str, endpoint: str) -> bool:
        return (
            identity in self.whitelisted_identities
            or endpoint in self.whitelisted_endpoints
        )

    # ---- 序列化 ----

    @staticmethod
    def _rule_from_dict(d: dict[str, Any]) -> RateLimitRule:
        return RateLimitRule(
            name=d["name"],
            identity=d.get("identity", "*"),
            endpoint=d.get("endpoint", "*"),
            strategy=StrategyType(d.get("strategy", StrategyType.FIXED_WINDOW.value)),
            limit=int(d.get("limit", 100)),
            window_seconds=float(d.get("window_seconds", 60.0)),
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TrafficGuardConfig":
        ban_data = data.get("ban", {})
        default_data = data.get("default_rule")
        return cls(
            rules=tuple(cls._rule_from_dict(r) for r in data.get("rules", [])),
            default_rule=(
                cls._rule_from_dict({**default_data, "name": default_data.get("name", "default")})
                if default_data
                else RateLimitRule(name="default")
            ),
            ban=BanConfig(
                enabled=bool(ban_data.get("enabled", True)),
                violation_threshold=int(ban_data.get("violation_threshold", 5)),
                violation_window_seconds=float(
                    ban_data.get("violation_window_seconds", 60.0)
                ),
                ban_seconds=float(ban_data.get("ban_seconds", 300.0)),
            ),
            degrade_mode=DegradeMode(
                data.get("degrade_mode", DegradeMode.FAIL_OPEN.value)
            ),
            whitelisted_identities=frozenset(data.get("whitelisted_identities", [])),
            whitelisted_endpoints=frozenset(data.get("whitelisted_endpoints", [])),
        )

    @classmethod
    def from_json_file(cls, path: str | Path) -> "TrafficGuardConfig":
        with open(path, encoding="utf-8") as f:
            return cls.from_dict(json.load(f))


class ConfigReloader:
    """基于文件 mtime 的配置热加载器。

    每次 get() 调用时检查文件修改时间，发生变化则重新解析；
    解析失败时保留上一份可用配置，保证服务不中断。
    """

    def __init__(self, path: str | Path, fallback: TrafficGuardConfig | None = None):
        self._path = Path(path)
        self._fallback = fallback or TrafficGuardConfig()
        self._lock = threading.Lock()
        self._mtime: float | None = None
        self._config: TrafficGuardConfig | None = None

    def get(self) -> TrafficGuardConfig:
        try:
            mtime = os.path.getmtime(self._path)
        except OSError:
            # 文件不可读时沿用已加载配置或兜底配置
            with self._lock:
                return self._config or self._fallback
        if self._config is not None and mtime == self._mtime:
            return self._config
        with self._lock:
            if self._config is not None and mtime == self._mtime:
                return self._config
            try:
                config = TrafficGuardConfig.from_json_file(self._path)
            except (OSError, ValueError, KeyError):
                return self._config or self._fallback
            self._config = config
            self._mtime = mtime
            return config
