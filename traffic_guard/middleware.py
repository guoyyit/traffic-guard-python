"""FastAPI/Starlette 限流中间件。

行为约定：
- 命中限流或封禁时返回稳定的 429 响应，JSON 体包含错误码、提示信息与
  retry_after 秒数，并携带 Retry-After 响应头供客户端退避；
- 放行请求附加 X-RateLimit-Limit / X-RateLimit-Remaining 响应头；
- 限流组件自身出现任何未预期异常时放行请求并记录日志，
  绝不把异常抛给请求方。
"""

from __future__ import annotations

import json
import logging
import math

from starlette.types import ASGIApp, Receive, Scope, Send

from traffic_guard.limiter import LimitResult, RateLimiter

logger = logging.getLogger("traffic_guard.middleware")

_STATUS_429 = 429


class RateLimitMiddleware:
    """纯 ASGI 限流中间件。

    identity_header 指定用于识别来源身份的请求头（如 "x-api-key"），
    缺省时回退到客户端 IP；endpoint 取请求路径。
    """

    def __init__(
        self,
        app: ASGIApp,
        limiter: RateLimiter,
        identity_header: str | None = None,
    ):
        self._app = app
        self._limiter = limiter
        self._identity_header = (
            identity_header.lower().encode("latin-1") if identity_header else None
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        identity = self._extract_identity(scope)
        endpoint = scope.get("path", "/")

        try:
            result = self._limiter.check(identity, endpoint)
        except Exception:  # noqa: BLE001 - 限流组件故障不得影响业务
            logger.exception("rate limiter failed, allowing request")
            result = LimitResult(allowed=True, reason="degraded_open")

        if not result.allowed:
            await self._send_429(send, result)
            return

        async def send_with_headers(message):
            if message["type"] == "http.response.start" and result.limit > 0:
                headers = list(message.get("headers", []))
                headers.append(
                    (b"x-ratelimit-limit", str(result.limit).encode("latin-1"))
                )
                headers.append(
                    (
                        b"x-ratelimit-remaining",
                        str(result.remaining).encode("latin-1"),
                    )
                )
                message["headers"] = headers
            await send(message)

        await self._app(scope, receive, send_with_headers)

    def _extract_identity(self, scope: Scope) -> str:
        headers = dict(scope.get("headers", []))
        if self._identity_header is not None:
            value = headers.get(self._identity_header)
            if value:
                return value.decode("latin-1")
        client = scope.get("client")
        if client:
            return client[0]
        return "anonymous"

    async def _send_429(self, send: Send, result: LimitResult) -> None:
        retry_after = max(math.ceil(result.retry_after), 1)
        body = json.dumps(
            {
                "error": "rate_limited",
                "message": "请求过于频繁，请稍后重试",
                "reason": result.reason,
                "retry_after": retry_after,
            }
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": _STATUS_429,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("latin-1")),
                    (b"retry-after", str(retry_after).encode("latin-1")),
                ],
            }
        )
        await send(
            {"type": "http.response.body", "body": body, "more_body": False}
        )
