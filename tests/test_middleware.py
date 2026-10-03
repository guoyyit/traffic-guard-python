"""中间件测试：确定性 429 响应、退避头、异常兜底、既有接口行为不变。"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from traffic_guard.config import (
    BanConfig,
    DegradeMode,
    RateLimitRule,
    TrafficGuardConfig,
)
from traffic_guard.limiter import RateLimiter
from traffic_guard.middleware import RateLimitMiddleware
from traffic_guard.storage import InMemoryBackend


def make_app(config, limiter=None):
    app = FastAPI()
    if limiter is None:
        limiter = RateLimiter(InMemoryBackend(), lambda: config)
    app.add_middleware(RateLimitMiddleware, limiter=limiter)

    @app.get("/")
    def root():
        return {"Hello": "World"}

    @app.get("/api/data")
    def data():
        return {"data": 42}

    return app, limiter


class TestDeterministic429:
    def test_limited_response_shape(self):
        cfg = TrafficGuardConfig(
            default_rule=RateLimitRule(name="d", limit=1, window_seconds=60),
            ban=BanConfig(enabled=False),
        )
        app, _ = make_app(cfg)
        client = TestClient(app)
        assert client.get("/").status_code == 200
        r = client.get("/")
        assert r.status_code == 429
        body = r.json()
        assert body["error"] == "rate_limited"
        assert body["message"]
        assert isinstance(body["retry_after"], int) and body["retry_after"] >= 1
        assert r.headers["retry-after"] == str(body["retry_after"])

    def test_retry_after_matches_window(self):
        cfg = TrafficGuardConfig(
            default_rule=RateLimitRule(name="d", limit=1, window_seconds=60),
            ban=BanConfig(enabled=False),
        )
        app, _ = make_app(cfg)
        client = TestClient(app)
        client.get("/")
        r = client.get("/")
        # 窗口 60s，retry_after 应在 (0, 60] 内
        assert 0 < int(r.headers["retry-after"]) <= 60

    def test_banned_response_reason(self):
        cfg = TrafficGuardConfig(
            default_rule=RateLimitRule(name="d", limit=1, window_seconds=60),
            ban=BanConfig(enabled=True, violation_threshold=2,
                          violation_window_seconds=60, ban_seconds=300),
        )
        app, _ = make_app(cfg)
        client = TestClient(app)
        client.get("/")
        client.get("/")
        r = client.get("/")
        assert r.status_code == 429
        assert r.json()["reason"] == "banned"

    def test_allowed_response_has_rate_limit_headers(self):
        cfg = TrafficGuardConfig(
            default_rule=RateLimitRule(name="d", limit=5, window_seconds=60)
        )
        app, _ = make_app(cfg)
        client = TestClient(app)
        r = client.get("/")
        assert r.status_code == 200
        assert r.headers["x-ratelimit-limit"] == "5"
        assert r.headers["x-ratelimit-remaining"] == "4"

    def test_existing_behavior_unchanged_under_limit(self):
        cfg = TrafficGuardConfig(
            default_rule=RateLimitRule(name="d", limit=100, window_seconds=60)
        )
        app, _ = make_app(cfg)
        client = TestClient(app)
        r = client.get("/")
        assert r.status_code == 200 and r.json() == {"Hello": "World"}


class TestIdentityDimensions:
    def test_identity_from_header(self):
        cfg = TrafficGuardConfig(
            default_rule=RateLimitRule(name="d", limit=1, window_seconds=60),
            ban=BanConfig(enabled=False),
        )
        app = FastAPI()
        limiter = RateLimiter(InMemoryBackend(), lambda: cfg)
        app.add_middleware(RateLimitMiddleware, limiter=limiter,
                           identity_header="x-api-key")

        @app.get("/")
        def root():
            return {"ok": True}

        client = TestClient(app)
        assert client.get("/", headers={"x-api-key": "key-a"}).status_code == 200
        assert client.get("/", headers={"x-api-key": "key-a"}).status_code == 429
        # 不同身份互不影响
        assert client.get("/", headers={"x-api-key": "key-b"}).status_code == 200

    def test_endpoints_limited_independently(self):
        cfg = TrafficGuardConfig(
            default_rule=RateLimitRule(name="d", limit=1, window_seconds=60),
            ban=BanConfig(enabled=False),
        )
        app, _ = make_app(cfg)
        client = TestClient(app)
        assert client.get("/").status_code == 200
        assert client.get("/api/data").status_code == 200  # 不同接口独立额度
        assert client.get("/").status_code == 429


class ExplodingLimiter:
    """check 必抛异常的限流器，验证中间件兜底。"""

    def check(self, identity, endpoint):
        raise RuntimeError("unexpected bug")


class TestMiddlewareResilience:
    def test_unexpected_error_allows_request(self):
        app, _ = make_app(TrafficGuardConfig(), limiter=ExplodingLimiter())
        client = TestClient(app, raise_server_exceptions=False)
        r = client.get("/")
        assert r.status_code == 200  # 限流组件故障不影响业务

    def test_fail_closed_returns_429_when_storage_down(self):
        from tests.test_limiter import FailingBackend

        cfg = TrafficGuardConfig(degrade_mode=DegradeMode.FAIL_CLOSED)
        limiter = RateLimiter(FailingBackend(), lambda: cfg)
        app, _ = make_app(cfg, limiter=limiter)
        client = TestClient(app)
        r = client.get("/")
        assert r.status_code == 429
        assert r.json()["reason"] == "degraded_closed"
        assert "retry-after" in r.headers
