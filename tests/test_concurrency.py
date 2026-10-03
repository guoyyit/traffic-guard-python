"""Concurrency tests: high contention must not cause quota penetration or corruption."""

from __future__ import annotations

import threading

from traffic_guard import SystemClock
from traffic_guard.config import LimitRule, StrategyName, TrafficGuardConfig
from traffic_guard.storage import MemoryStorage

from .conftest import make_guard

# Use a real clock and a long window so nothing expires mid-test.
WINDOW = 60.0


def _run_contention(guard, source: str, interface: str, threads: int, per_thread: int):
    barrier = threading.Barrier(threads)
    results: list[bool] = []
    lock = threading.Lock()

    def worker():
        barrier.wait()
        local = []
        for _ in range(per_thread):
            local.append(guard.check(source, interface).allowed)
        with lock:
            results.extend(local)

    workers = [threading.Thread(target=worker) for _ in range(threads)]
    for t in workers:
        t.start()
    for t in workers:
        t.join()
    return results


def test_fixed_window_exact_quota_under_contention():
    config = TrafficGuardConfig(
        default_limit=LimitRule(requests=50, window_seconds=WINDOW, strategy=StrategyName.FIXED_WINDOW)
    )
    guard, _, _ = make_guard(clock=SystemClock(), storage=MemoryStorage(), config=config)
    results = _run_contention(guard, "src", "/api", threads=25, per_thread=8)
    assert len(results) == 200
    assert sum(results) == 50  # exactly the quota, no penetration


def test_sliding_window_exact_quota_under_contention():
    config = TrafficGuardConfig(
        default_limit=LimitRule(requests=50, window_seconds=WINDOW, strategy=StrategyName.SLIDING_WINDOW)
    )
    guard, _, _ = make_guard(clock=SystemClock(), storage=MemoryStorage(), config=config)
    results = _run_contention(guard, "src", "/api", threads=25, per_thread=8)
    assert len(results) == 200
    assert sum(results) == 50


def test_different_keys_independent_under_contention():
    config = TrafficGuardConfig(
        default_limit=LimitRule(requests=10, window_seconds=WINDOW, strategy=StrategyName.FIXED_WINDOW)
    )
    guard, _, _ = make_guard(clock=SystemClock(), storage=MemoryStorage(), config=config)
    barrier = threading.Barrier(20)
    results: list[tuple[str, bool]] = []
    lock = threading.Lock()

    def worker(source):
        barrier.wait()
        local = [(source, guard.check(source, "/api").allowed) for _ in range(20)]
        with lock:
            results.extend(local)

    threads = [threading.Thread(target=worker, args=(f"src-{i}",)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    by_source = {}
    for source, allowed in results:
        by_source.setdefault(source, []).append(allowed)
    assert len(by_source) == 20
    for source, allowed in by_source.items():
        assert sum(allowed) == 10, f"{source} penetrated its quota"


def test_concurrent_violations_ban_once():
    # Many denied requests racing the ban path must not corrupt ban state or
    # crash; exactly one ban event is created.
    config = TrafficGuardConfig(
        default_limit=LimitRule(requests=1, window_seconds=WINDOW, strategy=StrategyName.FIXED_WINDOW),
    )
    config.ban.threshold = 3
    config.ban.ban_seconds = 60
    guard, _, _ = make_guard(clock=SystemClock(), storage=MemoryStorage(), config=config)
    results = _run_contention(guard, "src", "/api", threads=20, per_thread=5)
    assert sum(results) == 1  # only the very first request allowed
    banned = guard.check("src", "/api")
    assert banned.status_code == 403
    assert guard.metrics.snapshot()["bans_total"] == 1
