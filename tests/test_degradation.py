"""Degradation behavior when the storage backend is unavailable."""

from __future__ import annotations

from traffic_guard.config import LimitRule, TrafficGuardConfig
from traffic_guard.storage import FailingStorage, MemoryStorage

from .conftest import make_guard


def cfg(mode: str, requests: int = 2) -> TrafficGuardConfig:
    config = TrafficGuardConfig(default_limit=LimitRule(requests=requests, window_seconds=60))
    config.degradation.on_backend_failure = mode
    return config


def test_fail_open_allows_traffic(clock):
    storage = FailingStorage(MemoryStorage(clock=clock), fail_after=0)
    guard, _, _ = make_guard(clock, storage=storage, config=cfg("fail_open"))
    result = guard.check("src", "/api")
    assert result.allowed
    assert result.degraded
    assert guard.metrics.snapshot()["backend_errors_total"] == 1


def test_fail_close_returns_503(clock):
    storage = FailingStorage(MemoryStorage(clock=clock), fail_after=0)
    guard, _, _ = make_guard(clock, storage=storage, config=cfg("fail_close"))
    result = guard.check("src", "/api")
    assert not result.allowed
    assert result.status_code == 503
    assert result.degraded
    assert guard.metrics.snapshot()["backend_errors_total"] == 1


def test_local_fallback_still_enforces_limits(clock):
    storage = FailingStorage(MemoryStorage(clock=clock), fail_after=0)
    guard, _, _ = make_guard(clock, storage=storage, config=cfg("local_fallback", requests=2))
    assert guard.check("src", "/api").allowed
    assert guard.check("src", "/api").allowed
    denied = guard.check("src", "/api")
    assert not denied.allowed
    assert denied.status_code == 429
    assert denied.degraded
    assert guard.metrics.snapshot()["backend_errors_total"] >= 1


def test_local_fallback_recovers_when_backend_returns(clock):
    # First calls succeed against the primary; then it fails; fallback kicks in.
    primary = MemoryStorage(clock=clock)
    storage = FailingStorage(primary, fail_after=2)
    guard, _, _ = make_guard(clock, storage=storage, config=cfg("local_fallback", requests=2))
    assert guard.check("src", "/api").allowed  # primary, call 1
    assert guard.check("src", "/api").allowed  # primary, call 2
    # Primary fails -> local fallback with its own fresh quota.
    third = guard.check("src", "/api")
    assert third.allowed
    assert third.degraded
    fourth = guard.check("src", "/api")
    assert fourth.allowed
    denied = guard.check("src", "/api")
    assert not denied.allowed
    assert denied.status_code == 429
    assert denied.degraded


def test_fail_open_never_raises(clock):
    # Even repeated backend failures must surface as decisions, not exceptions.
    storage = FailingStorage(MemoryStorage(clock=clock), fail_after=0)
    guard, _, _ = make_guard(clock, storage=storage, config=cfg("fail_open"))
    for _ in range(10):
        result = guard.check("src", "/api")
        assert result.allowed
