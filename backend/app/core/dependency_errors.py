"""Dependency outages become a stable 503/504 instead of a generic 500.

A request that fails only because PostgreSQL, Redis or Qdrant cannot be reached (refused, reset,
timed out, pool exhausted) is not a bug in the request: the person gets `SERVICE_UNAVAILABLE` (or
`UPSTREAM_TIMEOUT`) with a request id, the technical detail goes to the log only, and a
`Retry-After` hint tells clients when to ask again. Programming errors keep the 500 path.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from qdrant_client.http.exceptions import ResponseHandlingException
from redis.exceptions import BusyLoadingError
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from app.core.exceptions import (
    ExternalServiceUnavailableError,
    UpstreamTimeoutError,
    app_error_response,
)
from app.core.logging import get_logger

log = get_logger(__name__)

RETRY_AFTER_S = 5

UNAVAILABLE: tuple[type[Exception], ...] = (
    ConnectionError,  # refused / reset / aborted (asyncpg and httpx raise these directly)
    OperationalError,  # SQLAlchemy: database unreachable or restarting
    InterfaceError,
    PoolTimeoutError,  # no free connection in the pool
    RedisConnectionError,
    BusyLoadingError,
    ResponseHandlingException,  # qdrant-client wraps transport errors in this
)
TIMED_OUT: tuple[type[Exception], ...] = (TimeoutError, RedisTimeoutError)


async def _unavailable(_: Request, exc: Exception) -> JSONResponse:
    log.error("dependency_unavailable", error=type(exc).__name__, exc_info=exc)
    return app_error_response(
        ExternalServiceUnavailableError(headers={"Retry-After": str(RETRY_AFTER_S)})
    )


async def _timed_out(_: Request, exc: Exception) -> JSONResponse:
    log.error("dependency_timeout", error=type(exc).__name__, exc_info=exc)
    return app_error_response(UpstreamTimeoutError(headers={"Retry-After": str(RETRY_AFTER_S)}))


def register_dependency_handlers(app: FastAPI) -> None:
    for exception_type in UNAVAILABLE:
        app.add_exception_handler(exception_type, _unavailable)
    for exception_type in TIMED_OUT:
        app.add_exception_handler(exception_type, _timed_out)
