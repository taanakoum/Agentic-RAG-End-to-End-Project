"""Pure ASGI middlewares (safe for streaming responses, unlike BaseHTTPMiddleware)."""

from __future__ import annotations

import json
from collections.abc import MutableMapping
from typing import Any

from app.core.request_context import (
    REQUEST_ID_HEADER,
    get_request_id,
    sanitize_request_id,
    set_request_id,
)

_Message = MutableMapping[str, Any]


class RequestIdMiddleware:
    """Accepts a well-formed X-Request-ID or generates one; echoes it on every response."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: _Message, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        incoming = next(
            (v.decode("latin-1") for k, v in scope["headers"] if k == b"x-request-id"), None
        )
        request_id = sanitize_request_id(incoming)
        set_request_id(request_id)
        from app.core.telemetry import set_attributes, trace_ids

        set_attributes(**{"app.request_id": request_id})  # joins the span with logs and the client
        ids = trace_ids()

        async def send_with_id(message: _Message) -> None:
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", []) if k != b"x-request-id"]
                headers.append((REQUEST_ID_HEADER.lower().encode(), request_id.encode()))
                if ids is not None:
                    headers.append((b"x-trace-id", ids[0].encode()))
                message["headers"] = headers
            await send(message)

        # Not reset afterwards: the outermost 500 handler still needs it, and every request runs in
        # its own task/context, so the value cannot leak between requests.
        await self.app(scope, receive, send_with_id)


def _too_large_body() -> bytes:
    return json.dumps(
        {
            "error": {
                "code": "PAYLOAD_TOO_LARGE",
                "message": "The request body is too large.",
                "details": [],
                "request_id": get_request_id(),
            }
        }
    ).encode()


class BodySizeLimitMiddleware:
    """Rejects bodies over the limit: by Content-Length up front, and while streaming."""

    def __init__(
        self, app: Any, *, max_body_bytes: int, path_limits: dict[str, int] | None = None
    ) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes
        self.path_limits = path_limits or {}

    def _limit_for(self, path: str) -> int:
        return self.path_limits.get(path, self.max_body_bytes)

    async def _reject(self, send: Any) -> None:
        body = _too_large_body()
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"connection", b"close"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope: _Message, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = self._limit_for(str(scope.get("path", "")))
        declared = next((v for k, v in scope["headers"] if k == b"content-length"), None)
        if declared is not None:
            try:
                if int(declared) > limit:
                    await self._reject(send)
                    return
            except ValueError:
                await self._reject(send)
                return

        received = 0
        responded = False
        rejected = False

        async def limited_receive() -> _Message:
            nonlocal received, responded, rejected
            if rejected:
                return {"type": "http.disconnect"}
            message: _Message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    # Answer 413 ourselves and tell the app the client is gone. (Raising here would
                    # be caught by FastAPI's body parsing and turned into a 400.)
                    rejected = True
                    if not responded:
                        responded = True
                        await self._reject(send)
                    return {"type": "http.disconnect"}
            return message

        async def tracking_send(message: _Message) -> None:
            nonlocal responded
            if rejected:
                return  # a 413 has already been sent; drop whatever the app tries to say
            if message["type"] == "http.response.start":
                responded = True
            await send(message)

        await self.app(scope, limited_receive, tracking_send)


_DOC_PATHS = ("/docs", "/openapi.json")


class SecurityHeadersMiddleware:
    """Defensive response headers for a JSON API (nothing here is ever rendered as a page).

    `no-store` keeps tokens and personal data out of shared caches and browser history; the CSP
    forbids everything (an API has no scripts, styles or frames); HSTS is only sent where TLS is
    terminated for the real deployment (`hsts=True`), never on plain-HTTP development.
    The interactive docs (`/docs`, development only) need scripts, so they get no CSP.
    """

    def __init__(self, app: Any, *, hsts: bool = False) -> None:
        self.app = app
        self.hsts = hsts

    async def __call__(self, scope: _Message, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = str(scope.get("path", ""))
        add: list[tuple[bytes, bytes]] = [
            (b"x-content-type-options", b"nosniff"),
            (b"referrer-policy", b"no-referrer"),
            (b"x-frame-options", b"DENY"),
            (b"cache-control", b"no-store"),
        ]
        if not path.startswith(_DOC_PATHS):
            add.append((b"content-security-policy", b"default-src 'none'; frame-ancestors 'none'"))
        if self.hsts:
            add.append((b"strict-transport-security", b"max-age=63072000; includeSubDomains"))

        async def send_with_headers(message: _Message) -> None:
            if message["type"] == "http.response.start":
                headers: list[tuple[bytes, bytes]] = list(message.get("headers", []))
                present = {k.lower() for k, _ in headers}
                headers.extend((k, v) for k, v in add if k not in present)  # never override
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)
