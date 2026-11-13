"""FastAPI application entry point."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.router import api_router
from app.chat.checkpointer import use_selector_loop
from app.core.config import Settings, get_settings
from app.core.dependency_errors import register_dependency_handlers
from app.core.exceptions import register_exception_handlers
from app.core.logging import configure_logging, get_logger
from app.core.middleware import (
    BodySizeLimitMiddleware,
    RequestIdMiddleware,
    SecurityHeadersMiddleware,
)
from app.core.telemetry import instrument_fastapi, setup_telemetry
from app.infra.container import Infrastructure
from app.retrieval.warmup import warm_models

use_selector_loop()  # psycopg async (LangGraph checkpointer) needs it on Windows


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(
        level=settings.log_level, json_output=settings.log_json, service=settings.service_name
    )
    log = get_logger(__name__)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        # asyncio.to_thread (storage, hashing, SMTP, outbox) shares this executor; the default
        # (cpu_count + 4) is too small for many simultaneous uploads.
        asyncio.get_running_loop().set_default_executor(
            ThreadPoolExecutor(max_workers=settings.thread_pool_size, thread_name_prefix="api")
        )
        infra = Infrastructure.create(settings)
        app.state.infra = infra
        log.info("startup_complete", env=settings.app_env)
        warmup: asyncio.Task[bool] | None = None
        if settings.warm_models_on_startup:
            warmup = asyncio.create_task(warm_models(settings, infra.inference_executor))
        # open the checkpointer pool in the background so the first chat turn does not wait for it
        # (the lazy path in `get` remains the fallback; this never blocks or fails start-up)
        checkpointer_warmup = asyncio.create_task(infra.checkpointer.warm_up())
        try:
            yield
        finally:
            checkpointer_warmup.cancel()
            if warmup is not None:
                warmup.cancel()
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
    register_dependency_handlers(app)
    if setup_telemetry(settings):  # tracing is off unless OTEL_ENABLED (or a test exporter is set)
        instrument_fastapi(app)
    # add_middleware: the last one added is outermost. Request ID must wrap everything else.
    app.add_middleware(
        BodySizeLimitMiddleware,
        max_body_bytes=settings.max_body_bytes,
        # multipart overhead on top of the PDF limit; the service enforces the exact PDF limit
        path_limits={
            "/api/v1/documents/upload": max(settings.max_pdf_bytes, settings.max_text_bytes)
            + 1024 * 1024
        },
    )
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_methods=["GET", "POST", "PATCH", "DELETE"],
            allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
        )
    app.add_middleware(SecurityHeadersMiddleware, hsts=settings.app_env == "prod")
    app.add_middleware(RequestIdMiddleware)
    app.include_router(api_router)
    return app


app = create_app()
