"""Shared test helpers."""

from __future__ import annotations

import pytest

from traffic_guard import FakeClock
from traffic_guard.config import ConfigLoader, TrafficGuardConfig
from traffic_guard.limiter import TrafficGuard
from traffic_guard.storage import MemoryStorage


@pytest.fixture
def clock() -> FakeClock:
    # Start at a non-zero, non-round timestamp to expose window-alignment bugs.
    return FakeClock(start=1000.0)


def make_guard(
    clock: FakeClock | None = None,
    storage=None,
    config: TrafficGuardConfig | None = None,
) -> tuple[TrafficGuard, FakeClock, MemoryStorage]:
    clock = clock or FakeClock(start=1000.0)
    storage = storage or MemoryStorage(clock=clock)
    loader = ConfigLoader.from_config(config or TrafficGuardConfig())
    guard = TrafficGuard(config_loader=loader, storage=storage, clock=clock)
    return guard, clock, storage
