"""Whitelist / exemption rules and hot reload."""

from __future__ import annotations

import json
import os

from traffic_guard import SystemClock
from traffic_guard.config import ConfigLoader, LimitRule, StrategyName, TrafficGuardConfig
from traffic_guard.limiter import TrafficGuard
from traffic_guard.storage import MemoryStorage

from .conftest import make_guard


def test_whitelisted_source_skips_limiting(clock):
    cfg = TrafficGuardConfig(default_limit=LimitRule(requests=1, window_seconds=60))
    cfg.whitelist_sources = {"vip"}
    guard, _, _ = make_guard(clock, config=cfg)
    for _ in range(10):
        assert guard.check("vip", "/api").allowed
    assert guard.check("normal", "/api").allowed  # quota still untouched
    assert not guard.check("normal", "/api").allowed


def test_whitelisted_interface_skips_limiting(clock):
    cfg = TrafficGuardConfig(default_limit=LimitRule(requests=1, window_seconds=60))
    cfg.whitelist_interfaces = {"/healthz"}
    guard, _, _ = make_guard(clock, config=cfg)
    for _ in range(10):
        assert guard.check("anyone", "/healthz").allowed
    assert guard.check("anyone", "/api").allowed  # quota still untouched
    assert not guard.check("anyone", "/api").allowed


def test_interface_specific_limit(clock):
    cfg = TrafficGuardConfig(default_limit=LimitRule(requests=50, window_seconds=60))
    cfg.interface_limits = {
        "/expensive": LimitRule(requests=1, window_seconds=60, strategy=StrategyName.SLIDING_WINDOW)
    }
    guard, _, _ = make_guard(clock, config=cfg)
    assert guard.check("src", "/expensive").allowed
    denied = guard.check("src", "/expensive")
    assert not denied.allowed
    assert denied.rule.strategy == StrategyName.SLIDING_WINDOW
    # Default interface still uses the default rule.
    assert guard.check("src", "/cheap").allowed


def test_whitelist_does_not_consume_quota(clock):
    cfg = TrafficGuardConfig(default_limit=LimitRule(requests=1, window_seconds=60))
    cfg.whitelist_interfaces = {"/healthz"}
    guard, _, storage = make_guard(clock, config=cfg)
    for _ in range(5):
        guard.check("src", "/healthz")
    assert guard.check("src", "/api").allowed  # quota untouched


def _write_config(path, **overrides):
    base = {
        "default_limit": {"requests": 2, "window_seconds": 60, "strategy": "fixed_window"},
        "whitelist_sources": [],
        "whitelist_interfaces": [],
        "ban": {"enabled": True, "threshold": 3, "violation_window_seconds": 60, "ban_seconds": 120},
        "degradation": {"on_backend_failure": "fail_open"},
    }
    base.update(overrides)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(base, fh)


def test_hot_reload_without_restart(tmp_path):
    path = tmp_path / "traffic_guard.json"
    _write_config(path, whitelist_sources=["a"])
    loader = ConfigLoader(str(path))
    guard = TrafficGuard(config_loader=loader, storage=MemoryStorage(), clock=SystemClock())

    assert guard.check("a", "/api").allowed  # whitelisted
    assert guard.check("b", "/api").allowed  # normal, quota 2
    assert guard.check("b", "/api").allowed
    assert not guard.check("b", "/api").allowed

    # Change the rules on disk: whitelist b as well.
    _write_config(path, whitelist_sources=["a", "b"])
    os.utime(path, None)
    result = guard.check("b", "/api")
    assert result.allowed
    metrics = guard.metrics.snapshot()
    assert metrics["whitelisted_total"] >= 2  # b's requests now whitelisted too


def test_hot_reload_bad_file_keeps_last_good_config(tmp_path):
    path = tmp_path / "traffic_guard.json"
    _write_config(path, whitelist_sources=["a"])
    loader = ConfigLoader(str(path))
    guard = TrafficGuard(config_loader=loader, storage=MemoryStorage(), clock=SystemClock())
    assert guard.check("a", "/api").allowed

    with open(path, "w", encoding="utf-8") as fh:
        fh.write("{not valid json")
    os.utime(path, None)
    # Reload failure: previous config stays in effect.
    assert guard.check("a", "/api").allowed
    assert loader.get().whitelist_sources == {"a"}
