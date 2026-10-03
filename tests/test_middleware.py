"""End-to-end middleware behavior via TestClient."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from traffic_guard import FakeClock, TrafficGuard, TrafficGuardMiddleware
from traffic_guard.config import ConfigLoader, LimitRule, StrategyName, TrafficGuardConfig
from traffic_guard.storage import MemoryStorage


def build_app(config: TrafficGuardConfig, clock: FakeClock) -> TestClient:
    guard = TrafficGuard(
        config_loader=ConfigLoader.from_config(config),
        storage=MemoryStorage(clock=clock),
        clock=clock,
    )
    app = FastAPI()
    app.add_middleware(TrafficGuardMiddleware, guard=guard)

    @app.get("/api")
    def api():
        return {"ok": True}

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    return TestClient(app), guard


def test_429_response_headers_and_body():
    clock = FakeClock(start=1000.0)
    config = TrafficGuardConfig(default_limit=LimitRule(requests=2, window_seconds=60))
    client, _ = build_app(config, clock)
    assert client.get("/api").status_code == 200
    assert client.get("/api").status_code == 200
    response = client.get("/api")
    assert response.status_code == 429
    assert "rate limit" in response.json()["detail"].lower()
    assert int(response.headers["retry-after"]) > 0
    assert response.headers["x-ratelimit-limit"] == "2"
    assert response.headers["x-ratelimit-remaining"] == "0"
    assert int(response.headers["x-ratelimit-reset"]) > 0


def test_banned_response_is_403():
    clock = FakeClock(start=1000.0)
    config = TrafficGuardConfig(default_limit=LimitRule(requests=1, window_seconds=60))
    config.ban.threshold = 2
    config.ban.ban_seconds = 120
    client, _ = build_app(config, clock)
    assert client.get("/api").status_code == 200
    assert client.get("/api").status_code == 429  # violation 1
    assert client.get("/api").status_code == 429  # violation 2 -> banned
    response = client.get("/api")
    assert response.status_code == 403
    assert "retry-after" in response.headers


def test_whitelisted_interface_bypasses_limiter():
    clock = FakeClock(start=1000.0)
    config = TrafficGuardConfig(default_limit=LimitRule(requests=1, window_seconds=60))
    config.whitelist_interfaces = {"/healthz"}
    client, _ = build_app(config, clock)
    for _ in range(5):
        assert client.get("/healthz").status_code == 200
    assert client.get("/api").status_code == 200
    assert client.get("/api").status_code == 429


def test_sliding_window_config_accepted():
    clock = FakeClock(start=1000.0)
    config = TrafficGuardConfig(
        default_limit=LimitRule(requests=3, window_seconds=60, strategy=StrategyName.SLIDING_WINDOW)
    )
    client, _ = build_app(config, clock)
    for _ in range(3):
        assert client.get("/api").status_code == 200
    assert client.get("/api").status_code == 429


def test_503_on_backend_fail_close():
    clock = FakeClock(start=1000.0)
    config = TrafficGuardConfig(default_limit=LimitRule(requests=2, window_seconds=60))
    config.degradation.on_backend_failure = "fail_close"
    guard = TrafficGuard(
        config_loader=ConfigLoader.from_config(config),
        storage=MemoryStorage(clock=clock),
        clock=clock,
    )
    # Swap in a failing storage after construction.
    from traffic_guard.storage import FailingStorage

    guard._storage = FailingStorage(guard._storage, fail_after=0)
    app = FastAPI()
    app.add_middleware(TrafficGuardMiddleware, guard=guard)

    @app.get("/api")
    def api():
        return {"ok": True}

    client = TestClient(app)
    response = client.get("/api")
    assert response.status_code == 503
    assert response.headers.get("x-traffic-guard-degraded") == "true"


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-v"]))


def test_middleware_fails_open_on_unexpected_error():
    clock = FakeClock(start=1000.0)
    config = TrafficGuardConfig(default_limit=LimitRule(requests=2, window_seconds=60))
    guard = TrafficGuard(
        config_loader=ConfigLoader.from_config(config),
        storage=MemoryStorage(clock=clock),
        clock=clock,
    )
    app = FastAPI()
    app.add_middleware(TrafficGuardMiddleware, guard=guard)

    @app.get("/api")
    def api():
        return {"ok": True}

    client = TestClient(app)
    # Break the limiter: unexpected errors must not surface as 500s.
    guard.check = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    response = client.get("/api")
    assert response.status_code == 200
    assert response.json() == {"ok": True}
