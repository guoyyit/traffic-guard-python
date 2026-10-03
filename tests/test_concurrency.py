"""并发竞争测试：高并发下额度不穿透、计数不错乱。"""

from concurrent.futures import ThreadPoolExecutor

from traffic_guard.config import BanConfig, RateLimitRule, StrategyType, TrafficGuardConfig
from traffic_guard.limiter import RateLimiter
from traffic_guard.storage import InMemoryBackend

_WORKERS = 8


def make_config(limit, strategy=StrategyType.FIXED_WINDOW):
    return TrafficGuardConfig(
        default_rule=RateLimitRule(
            name="d", strategy=strategy, limit=limit, window_seconds=3600
        ),
        ban=BanConfig(enabled=False),  # 专注计数竞争，排除封禁干扰
    )


def run_concurrent(limiter, identity, endpoint, n):
    with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
        return list(
            pool.map(lambda _: limiter.check(identity, endpoint).allowed, range(n))
        )


class TestConcurrency:
    def test_no_quota_penetration_single_key(self):
        limit = 50
        limiter = RateLimiter(InMemoryBackend(), lambda: make_config(limit))
        results = run_concurrent(limiter, "alice", "/api", n=500)
        allowed = sum(results)
        assert allowed == limit, f"放行 {allowed} 次，超过额度 {limit}"

    def test_independent_quotas_under_concurrency(self):
        limit = 20
        limiter = RateLimiter(InMemoryBackend(), lambda: make_config(limit))
        # 两个来源的请求交错并发提交，各自额度仍独立
        identities = ["alice" if i % 2 == 0 else "bob" for i in range(200)]
        with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
            results = list(
                pool.map(lambda ident: limiter.check(ident, "/api").allowed,
                         identities)
            )
        assert sum(results[0::2]) == limit
        assert sum(results[1::2]) == limit

    def test_both_strategies_race_safe(self):
        for strategy in (StrategyType.FIXED_WINDOW, StrategyType.SLIDING_WINDOW):
            limit = 30
            limiter = RateLimiter(
                InMemoryBackend(), lambda: make_config(limit, strategy)
            )
            results = run_concurrent(limiter, "alice", "/api", n=300)
            assert sum(results) == limit, f"{strategy} 出现额度穿透"
