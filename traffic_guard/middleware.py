"""ASGI middleware that enforces traffic guards before requests reach the app.

Rejected requests never reach the application, so they cannot consume backend
resources. Responses carry a stable JSON body plus ``Retry-After`` and the
standard ``X-RateLimit-*`` headers.
"""

from __future__ import annotations

import json
import logging
import math
from typing import Any

from .limiter import TrafficGuard

logger = logging.getLogger("traffic_guard")


class TrafficGuardMiddleware:
    def __init__(
        self,
        app: Any,
        guard: TrafficGuard,
        source_header: str | None = None,
    ) -> None:
        self.app = app
        self.guard = guard
        self.source_header = source_header

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        try:
            source = self._resolve_source(scope)
            interface = scope.get("path", "/")
            result = self.guard.check(source, interface)
        except Exception:  # noqa: BLE001 — never let the limiter break traffic
            logger.exception("traffic guard check failed; failing open")
            await self.app(scope, receive, send)
            return

        if result.allowed:
            await self.app(scope, receive, send)
            return

        await self._send_rejection(send, result)

    def _resolve_source(self, scope) -> str:
        if self.source_header is not None:
            header_name = self.source_header.lower().encode("latin-1")
            for name, value in scope.get("headers", []):
                if name == header_name:
                    return value.decode("latin-1")
        client = scope.get("client")
        if client and client[0]:
            return client[0]
        return "unknown"

    @staticmethod
    async def _send_rejection(send, result) -> None:
        retry_after = result.retry_after
        retry_after_header = str(max(1, math.ceil(retry_after))) if retry_after else "1"
        body = {
            "detail": result.message,
            "status_code": result.status_code,
            "retry_after_seconds": retry_after_header,
        }
        headers = [
            (b"content-type", b"application/json"),
            (b"retry-after", retry_after_header.encode("latin-1")),
            (b"x-ratelimit-limit", str(result.limit if result.limit is not None else 0).encode()),
            (b"x-ratelimit-remaining", str(result.remaining if result.remaining is not None else 0).encode()),
            (b"x-ratelimit-reset", str(math.ceil(result.reset_at) if result.reset_at else 0).encode()),
        ]
        if result.degraded:
            headers.append((b"x-traffic-guard-degraded", b"true"))
        payload = json.dumps(body).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": result.status_code,
                "headers": headers,
            }
        )
        await send({"type": "http.response.body", "body": payload})
