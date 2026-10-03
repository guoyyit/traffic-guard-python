"""FastAPI application with traffic guard wired in.

Configuration is read from ``traffic_guard.json`` if present (or the path in
``TRAFFIC_GUARD_CONFIG``). The file is watched and hot-reloaded, so whitelist
and rule changes apply without restarting.
"""

from __future__ import annotations

import logging
import os

from fastapi import FastAPI

from traffic_guard import TrafficGuard, TrafficGuardConfig, TrafficGuardMiddleware
from traffic_guard.config import ConfigLoader
from traffic_guard.storage import MemoryStorage

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

CONFIG_PATH = os.environ.get("TRAFFIC_GUARD_CONFIG", "traffic_guard.json")

loader = ConfigLoader(CONFIG_PATH if os.path.exists(CONFIG_PATH) else None)
guard = TrafficGuard(config_loader=loader, storage=MemoryStorage())

app = FastAPI(title="traffic-guard-python")
app.add_middleware(TrafficGuardMiddleware, guard=guard)


@app.get("/")
def read_root():
    return {"Hello": "World"}


@app.get("/healthz")
def healthz():
    # Health checks are typically whitelisted in the config file.
    return {"status": "ok"}


@app.get("/traffic-guard/status")
def traffic_guard_status():
    """Observability endpoint: counters, active bans and current config."""
    config: TrafficGuardConfig = loader.get()
    return {
        "metrics": guard.metrics.snapshot(),
        "active_bans": [
            {"source": b.source, "until": b.until, "reason": b.reason}
            for b in guard.ban_manager.list_bans()
        ],
        "recent_events": [str(e) for e in guard.ban_manager.recent_events()],
        "config": {
            "default_limit": config.default_limit.model_dump(mode="json"),
            "interface_limits": {
                path: rule.model_dump(mode="json")
                for path, rule in config.interface_limits.items()
            },
            "whitelist_sources": sorted(config.whitelist_sources),
            "whitelist_interfaces": sorted(config.whitelist_interfaces),
            "ban": config.ban.model_dump(),
            "degradation": config.degradation.model_dump(),
        },
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
