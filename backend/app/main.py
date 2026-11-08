"""FastAPI application entry point."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.router import api_router
from app.core.config import Settings, get_settings
from app.core.exceptions import register_exception_handlers
from app.core.logging import configure_logging, get_logger
from app.core.middleware import BodySizeLimitMiddleware, RequestIdMiddleware
from app.infra.container import Infrastructure


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
    register_exception_handlers(app)
    # add_middleware: the last one added is outermost. Request ID must wrap everything else.
    app.add_middleware(
        BodySizeLimitMiddleware,
        max_body_bytes=settings.max_body_bytes,
        # multipart overhead on top of the PDF limit; the service enforces the exact PDF limit
        path_limits={"/api/v1/documents/upload": settings.max_pdf_bytes + 1024 * 1024},
    )
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_methods=["GET", "POST", "PATCH", "DELETE"],
            allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
        )
    app.add_middleware(RequestIdMiddleware)
    app.include_router(api_router)
    return app


app = create_app()
