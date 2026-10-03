"""Sliding-window (log) strategy semantics."""

from __future__ import annotations

from traffic_guard.config import LimitRule, StrategyName, TrafficGuardConfig

from .conftest import make_guard


def rule(requests: int = 2, window: float = 10.0) -> LimitRule:
    return LimitRule(requests=requests, window_seconds=window, strategy=StrategyName.SLIDING_WINDOW)


def test_allows_then_limits(clock):
    guard, _, _ = make_guard(clock, config=TrafficGuardConfig(default_limit=rule()))
    assert guard.check("src", "/api").allowed
    assert guard.check("src", "/api").allowed
    denied = guard.check("src", "/api")
    assert not denied.allowed
    assert denied.status_code == 429
    assert denied.remaining == 0
    assert 0 < denied.retry_after <= 10


def test_slot_frees_after_oldest_request_leaves_window(clock):
    guard, _, _ = make_guard(clock, config=TrafficGuardConfig(default_limit=rule(requests=2, window=10)))
    assert guard.check("src", "/api").allowed
    assert guard.check("src", "/api").allowed
    clock.advance(5.0)
    assert not guard.check("src", "/api").allowed  # both timestamps still in window
    clock.advance(5.0)  # oldest timestamp is now exactly 10s old -> leaves
    assert guard.check("src", "/api").allowed


def test_retry_after_tracks_oldest_timestamp(clock):
    guard, _, _ = make_guard(clock, config=TrafficGuardConfig(default_limit=rule(requests=1, window=10)))
    assert guard.check("src", "/api").allowed
    clock.advance(3.0)
    denied = guard.check("src", "/api")
    assert not denied.allowed
    # The oldest (and only) timestamp at t=1000 leaves at t=1010; now is 1003.
    assert denied.retry_after == 7.0


def test_no_alignment_boundary(clock):
    # Unlike fixed windows, sliding windows never reset on a grid boundary.
    guard, _, _ = make_guard(clock, config=TrafficGuardConfig(default_limit=rule(requests=2, window=10)))
    assert guard.check("src", "/api").allowed
    assert guard.check("src", "/api").allowed
    clock.advance(9.99)
    assert not guard.check("src", "/api").allowed
    clock.advance(0.01)  # t=1010: oldest timestamp exactly 10s old -> gone
    assert guard.check("src", "/api").allowed


def test_sliding_vs_fixed_burst_difference(clock):
    # Fixed windows allow bursts across a boundary (2 at t=1009.9 + 2 at
    # t=1010.1); sliding windows keep the rate at 2 per 10s window.
    guard, _, _ = make_guard(clock, config=TrafficGuardConfig(default_limit=rule(requests=2, window=10)))
    assert guard.check("src", "/api").allowed
    clock.advance(9.9)
    assert guard.check("src", "/api").allowed
    clock.advance(0.2)
    assert guard.check("src", "/api").allowed  # oldest timestamp just aged out
    fourth = guard.check("src", "/api")
    assert not fourth.allowed
    assert 0 < fourth.retry_after <= 10


def test_eviction_does_not_drop_unexpired_keys(clock):
    # With a tiny max_keys, eviction runs on every write, but unexpired keys
    # must keep their history (regression: eviction used ts[-1] <= now).
    from traffic_guard.storage import MemoryStorage

    storage = MemoryStorage(max_keys=1, clock=clock)
    storage.sliding_record("k0", now=1000.0, window_seconds=10.0)
    storage.sliding_record("k1", now=1005.0, window_seconds=10.0)  # triggers eviction
    record = storage.sliding_record("k0", now=1005.0, window_seconds=10.0)
    assert record.count == 2  # k0's t=1000 timestamp survived
