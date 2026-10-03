"""限流核心语义：二维额度隔离、白名单豁免、配置热更新。"""

import json

import pytest

from traffic_guard.config import (
    ConfigReloader,
    DegradeMode,
    RateLimitRule,
    TrafficGuardConfig,
)
from traffic_guard.limiter import RateLimiter
from traffic_guard.storage import InMemoryBackend, StorageBackend


def make_limiter(config, clock=None):
    storage = InMemoryBackend()
    limiter = RateLimiter(storage, lambda: config, clock=clock or (lambda: 0.0))
    return limiter


class TestTwoDimensionalIsolation:
    def test_same_identity_different_endpoints_independent(self):
        cfg = TrafficGuardConfig(
            default_rule=RateLimitRule(name="d", limit=1, window_seconds=60)
        )
        limiter = make_limiter(cfg)
        assert limiter.check("alice", "/a").allowed
        assert limiter.check("alice", "/b").allowed  # 不同接口互不干扰
        assert not limiter.check("alice", "/a").allowed

    def test_different_identities_independent(self):
        cfg = TrafficGuardConfig(
            default_rule=RateLimitRule(name="d", limit=1, window_seconds=60)
        )
        limiter = make_limiter(cfg)
        assert limiter.check("alice", "/a").allowed
        assert not limiter.check("alice", "/a").allowed
        assert limiter.check("bob", "/a").allowed  # 不同来源额度独立

    def test_per_rule_limits(self):
        cfg = TrafficGuardConfig(
            rules=(
                RateLimitRule(name="vip", identity="vip-user", limit=5,
                              window_seconds=60),
                RateLimitRule(name="api", endpoint="/heavy", limit=1,
                              window_seconds=60),
            ),
            default_rule=RateLimitRule(name="d", limit=3, window_seconds=60),
        )
        limiter = make_limiter(cfg)
        # 普通用户走默认规则
        for _ in range(3):
            assert limiter.check("alice", "/light").allowed
        assert not limiter.check("alice", "/light").allowed
        # vip 用户走专属规则
        for _ in range(5):
            assert limiter.check("vip-user", "/light").allowed
        assert not limiter.check("vip-user", "/light").allowed
        # 特定接口走接口级规则
        assert limiter.check("alice", "/heavy").allowed
        assert not limiter.check("alice", "/heavy").allowed


class TestWhitelist:
    def test_whitelisted_identity_skips_limit(self):
        cfg = TrafficGuardConfig(
            default_rule=RateLimitRule(name="d", limit=1, window_seconds=60),
            whitelisted_identities=frozenset({"internal-bot"}),
        )
        limiter = make_limiter(cfg)
        for _ in range(10):
            r = limiter.check("internal-bot", "/a")
            assert r.allowed and r.reason == "whitelisted"

    def test_whitelisted_endpoint_skips_limit(self):
        cfg = TrafficGuardConfig(
            default_rule=RateLimitRule(name="d", limit=1, window_seconds=60),
            whitelisted_endpoints=frozenset({"/health"}),
        )
        limiter = make_limiter(cfg)
        for _ in range(10):
            assert limiter.check("alice", "/health").allowed
        assert limiter.check("alice", "/api").allowed
        assert not limiter.check("alice", "/api").allowed


class TestHotReload:
    def test_rule_change_takes_effect_without_restart(self, tmp_path):
        path = tmp_path / "cfg.json"
        path.write_text(json.dumps({
            "default_rule": {"name": "d", "limit": 1, "window_seconds": 60},
        }))
        reloader = ConfigReloader(path)
        limiter = RateLimiter(InMemoryBackend(), reloader.get, clock=lambda: 0.0)

        assert limiter.check("alice", "/a").allowed
        assert not limiter.check("alice", "/a").allowed

        # 调整配置（加入白名单），无需重启即生效
        import os
        path.write_text(json.dumps({
            "default_rule": {"name": "d", "limit": 1, "window_seconds": 60},
            "whitelisted_identities": ["alice"],
        }))
        os.utime(path, (1e9, 1e9))  # 确保 mtime 变化
        r = limiter.check("alice", "/a")
        assert r.allowed and r.reason == "whitelisted"

    def test_broken_config_keeps_last_good(self, tmp_path):
        path = tmp_path / "cfg.json"
        path.write_text(json.dumps({
            "default_rule": {"name": "d", "limit": 1, "window_seconds": 60},
        }))
        reloader = ConfigReloader(path)
        good = reloader.get()
        path.write_text("{not valid json")
        import os
        os.utime(path, (2e9, 2e9))
        assert reloader.get() is good  # 解析失败沿用上一份可用配置


class FailingBackend(StorageBackend):
    """所有操作都抛 StorageError 的故障后端。"""

    def apply(self, key, fn):
        from traffic_guard.storage import StorageError
        raise StorageError("backend down")

    def get(self, key):
        from traffic_guard.storage import StorageError
        raise StorageError("backend down")

    def set(self, key, value, ttl=None):
        from traffic_guard.storage import StorageError
        raise StorageError("backend down")

    def delete(self, key):
        from traffic_guard.storage import StorageError
        raise StorageError("backend down")


class TestDegradation:
    def test_fail_open_allows_when_backend_down(self):
        cfg = TrafficGuardConfig(degrade_mode=DegradeMode.FAIL_OPEN)
        limiter = RateLimiter(FailingBackend(), lambda: cfg)
        r = limiter.check("alice", "/a")
        assert r.allowed and r.reason == "degraded_open"

    def test_fail_closed_denies_with_retry_hint(self):
        cfg = TrafficGuardConfig(degrade_mode=DegradeMode.FAIL_CLOSED)
        limiter = RateLimiter(FailingBackend(), lambda: cfg)
        r = limiter.check("alice", "/a")
        assert not r.allowed
        assert r.reason == "degraded_closed"
        assert r.retry_after > 0

    def test_backend_error_never_propagates(self):
        cfg = TrafficGuardConfig()
        limiter = RateLimiter(FailingBackend(), lambda: cfg)
        # 不抛任何异常
        limiter.check("alice", "/a")
