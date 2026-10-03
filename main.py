import os

import uvicorn
from fastapi import FastAPI

from traffic_guard import (
    ConfigReloader,
    InMemoryBackend,
    RateLimiter,
    RateLimitMiddleware,
    TrafficGuardConfig,
)

# 配置文件路径可通过环境变量指定；文件变更后无需重启即可生效
_CONFIG_PATH = os.environ.get("TRAFFIC_GUARD_CONFIG", "traffic_guard_config.json")

if os.path.exists(_CONFIG_PATH):
    _reloader = ConfigReloader(_CONFIG_PATH)
else:
    _default_config = TrafficGuardConfig()
    _reloader = ConfigReloader(_CONFIG_PATH, fallback=_default_config)

_limiter = RateLimiter(InMemoryBackend(), _reloader.get)

app = FastAPI()
app.add_middleware(RateLimitMiddleware, limiter=_limiter)


@app.get("/")
def read_root():
    return {"Hello": "World"}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=0)
