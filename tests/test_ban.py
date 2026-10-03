"""Ban and auto-unban behavior."""

from __future__ import annotations

from traffic_guard.config import LimitRule, TrafficGuardConfig

from .conftest import make_guard


def config(threshold: int = 3, ban_seconds: float = 120.0) -> TrafficGuardConfig:
    cfg = TrafficGuardConfig(default_limit=LimitRule(requests=2, window_seconds=60))
    cfg.ban.threshold = threshold
    cfg.ban.violation_window_seconds = 60
    cfg.ban.ban_seconds = ban_seconds
    return cfg


def test_repeated_violations_lead_to_ban(clock):
    guard, _, _ = make_guard(clock, config=config())
    assert guard.check("src", "/api").allowed
    assert guard.check("src", "/api").allowed
    for _ in range(3):
        result = guard.check("src", "/api")
        assert result.status_code == 429
    banned = guard.check("src", "/api")
    assert banned.status_code == 403
    assert not banned.allowed
    assert 0 < banned.retry_after <= 120
    metrics = guard.metrics.snapshot()
    assert metrics["bans_total"] == 1
    assert metrics["banned_total"] >= 1


def test_banned_requests_do_not_touch_storage(clock):
    cfg = config()
    guard, _, storage = make_guard(clock, config=cfg)
    assert guard.check("src", "/api").allowed
    assert guard.check("src", "/api").allowed
    for _ in range(3):
        guard.check("src", "/api")
    assert guard.check("src", "/api").status_code == 403
    # A banned request must not create or increment any counter in storage.
    keys_before = len(storage._backend._fixed)
    guard.check("src", "/api")
    guard.check("src", "/other")  # different interface, same source: also banned
    assert len(storage._backend._fixed) == keys_before


def test_auto_unban_after_expiry(clock):
    guard, _, _ = make_guard(clock, config=config(ban_seconds=120))
    assert guard.check("src", "/api").allowed
    assert guard.check("src", "/api").allowed
    for _ in range(3):
        guard.check("src", "/api")
    assert guard.check("src", "/api").status_code == 403
    clock.advance(119.9)
    assert guard.check("src", "/api").status_code == 403  # still banned
    clock.advance(0.1)  # ban expires exactly now
    result = guard.check("src", "/api")
    assert result.allowed  # unbanned
    metrics = guard.metrics.snapshot()
    assert metrics["bans_unbanned_total"] == 1


def test_ban_is_per_source(clock):
    guard, _, _ = make_guard(clock, config=config())
    assert guard.check("a", "/api").allowed
    assert guard.check("a", "/api").allowed
    for _ in range(3):
        guard.check("a", "/api")
    assert guard.check("a", "/api").status_code == 403
    # Source b is unaffected.
    assert guard.check("b", "/api").allowed


def test_violations_age_out_of_window(clock):
    # Violations older than violation_window_seconds do not accumulate into a ban.
    cfg = config(threshold=3, ban_seconds=120)
    guard, _, _ = make_guard(clock, config=cfg)
    def burst():
        guard.check("src", "/api")
        guard.check("src", "/api")
        guard.check("src", "/api")  # 2 allowed + 1 violation

    burst()  # violation 1
    clock.advance(61)
    burst()  # violation 2 (violation 1 aged out)
    clock.advance(61)
    burst()  # violation 3 (violation 2 aged out)
    assert guard.check("src", "/api").status_code == 429  # not banned


def test_observability_events(clock):
    guard, _, _ = make_guard(clock, config=config(ban_seconds=10))
    assert guard.check("src", "/api").allowed
    assert guard.check("src", "/api").allowed
    for _ in range(3):
        guard.check("src", "/api")
    events = guard.ban_manager.recent_events()
    assert any(e.__class__.__name__ == "BanEvent" for e in events)
    clock.advance(10.1)
    guard.check("src", "/api")
    events = guard.ban_manager.recent_events()
    assert any(e.__class__.__name__ == "UnbanEvent" for e in events)


def test_ban_preserved_across_config_reload(tmp_path):
    import json
    import os

    from traffic_guard import SystemClock, TrafficGuard
    from traffic_guard.config import ConfigLoader
    from traffic_guard.storage import MemoryStorage

    path = tmp_path / "tg.json"
    path.write_text(json.dumps({
        "default_limit": {"requests": 1, "window_seconds": 60},
        "ban": {"enabled": True, "threshold": 2, "violation_window_seconds": 60, "ban_seconds": 120},
    }))
    loader = ConfigLoader(str(path))
    guard = TrafficGuard(config_loader=loader, storage=MemoryStorage(), clock=SystemClock())
    guard.check("src", "/api")  # allowed
    guard.check("src", "/api")  # violation 1
    guard.check("src", "/api")  # violation 2 -> banned
    assert guard.check("src", "/api").status_code == 403

    # Reload with a different ban config; the in-progress ban must survive.
    path.write_text(json.dumps({
        "default_limit": {"requests": 1, "window_seconds": 60},
        "ban": {"enabled": True, "threshold": 99, "violation_window_seconds": 60, "ban_seconds": 120},
    }))
    os.utime(path, None)
    assert guard.check("src", "/api").status_code == 403
