"""Fixed-window strategy semantics and boundary behavior."""

from __future__ import annotations

from traffic_guard.config import LimitRule, StrategyName, TrafficGuardConfig

from .conftest import make_guard


def rule(requests: int = 3, window: float = 10.0) -> LimitRule:
    return LimitRule(requests=requests, window_seconds=window, strategy=StrategyName.FIXED_WINDOW)


def test_allows_up_to_limit_then_rejects(clock):
    guard, _, _ = make_guard(clock, config=TrafficGuardConfig(default_limit=rule()))
    for _ in range(3):
        assert guard.check("src", "/api").allowed
    result = guard.check("src", "/api")
    assert not result.allowed
    assert result.status_code == 429
    assert result.remaining == 0
    assert result.limit == 3
    assert 0 < result.retry_after <= 10


def test_retry_after_shrinks_toward_boundary(clock):
    guard, _, _ = make_guard(clock, config=TrafficGuardConfig(default_limit=rule(requests=1, window=10)))
    assert guard.check("src", "/api").allowed
    denied = guard.check("src", "/api")
    assert denied.retry_after == 10.0
    clock.advance(4.0)
    denied = guard.check("src", "/api")
    assert denied.retry_after == 6.0


def test_sources_and_interfaces_are_independent(clock):
    guard, _, _ = make_guard(clock, config=TrafficGuardConfig(default_limit=rule(requests=1)))
    assert guard.check("a", "/x").allowed
    assert not guard.check("a", "/x").allowed
    # Different source: independent quota.
    assert guard.check("b", "/x").allowed
    # Different interface for same source: independent quota.
    assert guard.check("a", "/y").allowed


def test_boundary_resets_counter(clock):
    guard, _, _ = make_guard(clock, config=TrafficGuardConfig(default_limit=rule(requests=2, window=10)))
    assert guard.check("src", "/api").allowed
    assert guard.check("src", "/api").allowed
    assert not guard.check("src", "/api").allowed
    # Exactly at the next window boundary: a fresh window starts.
    clock.advance(10.0)
    assert guard.check("src", "/api").allowed
    assert guard.check("src", "/api").allowed
    assert not guard.check("src", "/api").allowed


def test_request_just_before_boundary_stays_in_old_window(clock):
    guard, _, _ = make_guard(clock, config=TrafficGuardConfig(default_limit=rule(requests=1, window=10)))
    assert guard.check("src", "/api").allowed
    clock.advance(9.9)
    assert not guard.check("src", "/api").allowed  # still old window
    clock.advance(0.1)  # now exactly at boundary
    assert guard.check("src", "/api").allowed  # new window


def test_burst_across_boundary_is_allowed_for_fixed_window(clock):
    # Fixed windows allow bursts at boundaries: 2 requests in the last instant
    # of one window and 2 more at the start of the next are both allowed.
    guard, _, _ = make_guard(clock, config=TrafficGuardConfig(default_limit=rule(requests=2, window=10)))
    assert guard.check("src", "/api").allowed
    clock.advance(9.99)
    assert guard.check("src", "/api").allowed
    clock.advance(0.01)
    assert guard.check("src", "/api").allowed
    assert guard.check("src", "/api").allowed
