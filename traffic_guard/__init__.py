"""Traffic guard: configurable rate limiting and abuse protection for FastAPI."""

from __future__ import annotations

__all__ = [
    "TrafficGuard",
    "TrafficGuardMiddleware",
    "TrafficGuardConfig",
    "SystemClock",
    "FakeClock",
]


def __getattr__(name):
    # Lazy re-exports to keep package import light and avoid circular imports.
    if name == "TrafficGuard":
        from .limiter import TrafficGuard

        return TrafficGuard
    if name == "TrafficGuardMiddleware":
        from .middleware import TrafficGuardMiddleware

        return TrafficGuardMiddleware
    if name == "TrafficGuardConfig":
        from .config import TrafficGuardConfig

        return TrafficGuardConfig
    if name == "SystemClock":
        from .clock import SystemClock

        return SystemClock
    if name == "FakeClock":
        from .clock import FakeClock

        return FakeClock
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
