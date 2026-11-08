"""FastAPI application entry point."""

from __future__ import annotations

from collections.abc import AsyncGenerator, MutableMapping
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI

from app.api.v1.router import api_router
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging, get_logger
from app.core.request_context import (
    REQUEST_ID_HEADER,
    sanitize_request_id,
    set_request_id,
)
from app.infra.container import Infrastructure

_Scope = MutableMapping[str, Any]


class RequestIdMiddleware:
    """Pure ASGI middleware (safe for SSE streaming, unlike BaseHTTPMiddleware)."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: _Scope, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        incoming = next(
            (v.decode("latin-1") for k, v in scope["headers"] if k == b"x-request-id"), None
        )
        request_id = sanitize_request_id(incoming)
        set_request_id(request_id)

        async def send_with_id(message: _Scope) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((REQUEST_ID_HEADER.lower().encode(), request_id.encode()))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            set_request_id(None)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(
        level=settings.log_level, json_output=settings.log_json, service=settings.service_name
    )
    log = get_logger(__name__)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        infra = Infrastructure.create(settings)
        app.state.infra = infra
        log.info("startup_complete", env=settings.app_env)
        try:
            yield
        finally:
            await infra.close()
            log.info("shutdown_complete")

    app = FastAPI(
        title="Agentic RAG Platform",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None if settings.app_env == "prod" else "/docs",
        redoc_url=None,
        openapi_url=None if settings.app_env == "prod" else "/openapi.json",
    )
    app.add_middleware(RequestIdMiddleware)
    app.include_router(api_router)
    return app


app = create_app()
