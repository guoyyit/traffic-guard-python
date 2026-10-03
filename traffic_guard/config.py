"""Rate-limit configuration models and hot-reloading loader.

Configuration is loaded from a JSON file (see ``traffic_guard.example.json``).
The loader watches the file's mtime and reloads it transparently, so whitelist
and rule changes take effect without restarting the service.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

logger = logging.getLogger("traffic_guard")


class StrategyName(str, Enum):
    FIXED_WINDOW = "fixed_window"
    SLIDING_WINDOW = "sliding_window"


class LimitRule(BaseModel):
    requests: int = Field(gt=0, description="Maximum requests allowed per window")
    window_seconds: float = Field(gt=0, description="Window length in seconds")
    strategy: StrategyName = StrategyName.FIXED_WINDOW


class BanConfig(BaseModel):
    enabled: bool = True
    threshold: int = Field(default=5, gt=0, description="Violations within violation_window before banning")
    violation_window_seconds: float = Field(default=60.0, gt=0)
    ban_seconds: float = Field(default=300.0, gt=0)


class DegradationConfig(BaseModel):
    """Behavior when the shared storage backend is unavailable."""

    on_backend_failure: Literal["fail_open", "fail_close", "local_fallback"] = "fail_open"
    local_fallback_max_keys: int = Field(default=10000, gt=0)


class TrafficGuardConfig(BaseModel):
    default_limit: LimitRule = Field(
        default_factory=lambda: LimitRule(requests=100, window_seconds=60.0)
    )
    interface_limits: dict[str, LimitRule] = Field(default_factory=dict)
    whitelist_sources: set[str] = Field(default_factory=set)
    whitelist_interfaces: set[str] = Field(default_factory=set)
    ban: BanConfig = Field(default_factory=BanConfig)
    degradation: DegradationConfig = Field(default_factory=DegradationConfig)

    def rule_for(self, interface: str) -> LimitRule:
        """Return the rule for an interface, falling back to the default."""
        return self.interface_limits.get(interface, self.default_limit)


def default_config() -> TrafficGuardConfig:
    return TrafficGuardConfig()


class ConfigLoader:
    """Loads config from a JSON file and hot-reloads it when the file changes.

    If ``path`` is None, the env var ``TRAFFIC_GUARD_CONFIG`` is consulted.
    A missing file yields the built-in defaults. Reload failures keep the
    last good config and are logged instead of raising.
    """

    def __init__(self, path: str | None = None) -> None:
        self._path = path or os.environ.get("TRAFFIC_GUARD_CONFIG")
        self._lock = threading.Lock()
        self._config: TrafficGuardConfig = default_config()
        self._mtime: float | None = None
        if self._path:
            self._reload(initial=True)

    @classmethod
    def from_config(cls, config: TrafficGuardConfig) -> "ConfigLoader":
        """Build a loader that serves a fixed config (no file watching)."""
        loader = cls.__new__(cls)
        loader._path = None
        loader._lock = threading.Lock()
        loader._config = config
        loader._mtime = None
        return loader

    @property
    def path(self) -> str | None:
        return self._path

    def get(self) -> TrafficGuardConfig:
        if self._path:
            self._maybe_reload()
        with self._lock:
            return self._config

    def _maybe_reload(self) -> None:
        try:
            mtime = os.stat(self._path).st_mtime  # type: ignore[arg-type]
        except OSError:
            return
        with self._lock:
            if self._mtime is not None and mtime == self._mtime:
                return
            self._load_locked()

    def _reload(self, initial: bool = False) -> None:
        with self._lock:
            self._load_locked(initial=initial)

    def _load_locked(self, initial: bool = False) -> None:
        try:
            with open(self._path, "r", encoding="utf-8") as fh:  # type: ignore[arg-type]
                raw = json.load(fh)
            config = TrafficGuardConfig.model_validate(raw)
            self._config = config
            self._mtime = os.stat(self._path).st_mtime  # type: ignore[arg-type]
            logger.info("traffic guard config loaded from %s", self._path)
        except FileNotFoundError:
            if initial:
                logger.warning("config file %s not found, using defaults", self._path)
        except (OSError, json.JSONDecodeError, ValidationError) as exc:
            logger.error("failed to load traffic guard config %s: %s; keeping last good config", self._path, exc)
