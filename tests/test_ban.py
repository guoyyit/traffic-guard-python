"""封禁与自动解封测试：触发条件、解封时机、可观测性、资源隔离。"""

import logging

from traffic_guard.ban import BanManager
from traffic_guard.config import BanConfig, RateLimitRule, TrafficGuardConfig
from traffic_guard.limiter import RateLimiter
from traffic_guard.storage import InMemoryBackend


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def make_limiter(clock, ban_cfg):
    cfg = TrafficGuardConfig(
        default_rule=RateLimitRule(name="d", limit=1, window_seconds=60),
        ban=ban_cfg,
    )
    storage = InMemoryBackend(clock=clock)
    return RateLimiter(storage, lambda: cfg, clock=clock), storage


BAN_CFG = BanConfig(
    enabled=True, violation_threshold=3, violation_window_seconds=60, ban_seconds=300
)


class TestBanTrigger:
    def test_banned_after_threshold_violations(self):
        clock = Clock()
        limiter, _ = make_limiter(clock, BAN_CFG)
        # limit=1：每秒一次的第 2 个请求即违规
        assert limiter.check("alice", "/a").allowed
        for expected_banned in (False, False, True):
            clock.advance(1)
            r = limiter.check("alice", "/a")
            assert not r.allowed
            assert (r.reason == "banned") == expected_banned

    def test_ban_is_per_identity(self):
        clock = Clock()
        limiter, _ = make_limiter(clock, BAN_CFG)
        limiter.check("alice", "/a")
        for _ in range(3):
            clock.advance(1)
            limiter.check("alice", "/a")
        assert limiter.check("alice", "/a").reason == "banned"
        assert limiter.check("bob", "/a").allowed  # bob 不受牵连

    def test_old_violations_expire(self):
        clock = Clock()
        limiter, _ = make_limiter(clock, BAN_CFG)
        limiter.check("alice", "/a")
        clock.advance(1)
        limiter.check("alice", "/a")  # 1 次违规
        clock.advance(120)  # 超出 violation_window_seconds，违规记录过期
        limiter.check("alice", "/a")
        clock.advance(1)
        r = limiter.check("alice", "/a")
        assert r.reason == "rate_limited"  # 未累计到阈值，不封禁


class TestAutoUnban:
    def test_auto_unban_after_ban_seconds(self):
        clock = Clock()
        limiter, _ = make_limiter(clock, BAN_CFG)
        limiter.check("alice", "/a")
        for _ in range(3):
            clock.advance(1)
            limiter.check("alice", "/a")
        assert limiter.check("alice", "/a").reason == "banned"
        clock.advance(301)
        assert limiter.check("alice", "/a").allowed  # 到期自动解封

    def test_ban_and_unban_logged(self, caplog):
        clock = Clock()
        limiter, _ = make_limiter(clock, BAN_CFG)
        with caplog.at_level(logging.INFO, logger="traffic_guard.ban"):
            limiter.check("alice", "/a")
            for _ in range(3):
                clock.advance(1)
                limiter.check("alice", "/a")
            clock.advance(301)
            limiter.check("alice", "/a")
        messages = [r.getMessage() for r in caplog.records]
        assert any("banned" in m and "alice" in m for m in messages)
        assert any("auto-unbanned" in m and "alice" in m for m in messages)


class SpyBackend:
    """包装存储后端，统计 apply 调用次数。"""

    def __init__(self, inner):
        self._inner = inner
        self.apply_calls = 0

    def apply(self, key, fn):
        self.apply_calls += 1
        return self._inner.apply(key, fn)

    def get(self, key):
        return self._inner.get(key)

    def set(self, key, value, ttl=None):
        return self._inner.set(key, value, ttl=ttl)

    def delete(self, key):
        return self._inner.delete(key)


class TestBanResourceIsolation:
    def test_banned_requests_skip_rate_counter(self):
        clock = Clock()
        cfg = TrafficGuardConfig(
            default_rule=RateLimitRule(name="d", limit=1, window_seconds=60),
            ban=BAN_CFG,
        )
        spy = SpyBackend(InMemoryBackend(clock=clock))
        limiter = RateLimiter(spy, lambda: cfg, clock=clock)
        limiter.check("alice", "/a")
        for _ in range(3):
            clock.advance(1)
            limiter.check("alice", "/a")
        assert limiter.check("alice", "/a").reason == "banned"
        calls_at_ban = spy.apply_calls
        # 封禁期间的请求不再触碰计数存储（apply 调用数不增长）
        for _ in range(10):
            clock.advance(1)
            r = limiter.check("alice", "/a")
            assert r.reason == "banned"
        assert spy.apply_calls == calls_at_ban

    def test_ban_disabled_by_config(self):
        clock = Clock()
        limiter, _ = make_limiter(clock, BanConfig(enabled=False))
        limiter.check("alice", "/a")
        for _ in range(10):
            clock.advance(1)
            r = limiter.check("alice", "/a")
            assert r.reason == "rate_limited"  # 始终只是限流，不封禁
