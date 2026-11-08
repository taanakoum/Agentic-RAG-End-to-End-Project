"""Dependency health checks. Failures are logged in detail but reported only as "error"."""

from __future__ import annotations

import asyncio
import io
import uuid
from collections.abc import Awaitable, Callable
from typing import Literal

from pydantic import BaseModel
from sqlalchemy import text

from app.core.logging import get_logger
from app.infra.clients import async_redis_ping
from app.infra.container import Infrastructure
from app.tasks.celery_app import celery_app

log = get_logger(__name__)

Status = Literal["ok", "error"]


class ComponentHealth(BaseModel):
    status: Status


class HealthReport(BaseModel):
    status: Status
    components: dict[str, ComponentHealth]


async def _check_postgres(infra: Infrastructure) -> None:
    async with infra.session_factory() as session:
        await session.execute(text("SELECT 1"))


async def _check_redis(infra: Infrastructure) -> None:
    if not await async_redis_ping(infra.redis):
        raise RuntimeError("redis ping returned falsy")


async def _check_qdrant(infra: Infrastructure) -> None:
    await infra.qdrant.get_collections()


def _storage_probe(infra: Infrastructure) -> None:
    key = f"_health/{uuid.uuid4().hex}"
    payload = b"ok"
    infra.storage.put(key, io.BytesIO(payload))
    try:
        with infra.storage.open(key) as handle:
            if handle.read() != payload:
                raise RuntimeError("storage read-back mismatch")
    finally:
        infra.storage.delete(key)


async def _check_storage(infra: Infrastructure) -> None:
    await asyncio.to_thread(_storage_probe, infra)


def _ping_workers(timeout: float) -> int:
    replies = celery_app.control.inspect(timeout=timeout).ping() or {}
    return len(replies)


async def _check_workers(infra: Infrastructure) -> None:
    count = await asyncio.to_thread(_ping_workers, infra.settings.celery_health_timeout_s)
    if count == 0:
        raise RuntimeError("no celery workers responded")


_READY_CHECKS: dict[str, Callable[[Infrastructure], Awaitable[None]]] = {
    "postgres": _check_postgres,
    "redis": _check_redis,
    "qdrant": _check_qdrant,
    "storage": _check_storage,
}


async def _run(
    name: str, check: Callable[[Infrastructure], Awaitable[None]], infra: Infrastructure
) -> ComponentHealth:
    try:
        await asyncio.wait_for(check(infra), timeout=10)
    except Exception:
        log.exception("health_check_failed", component=name)
        return ComponentHealth(status="error")
    return ComponentHealth(status="ok")


async def _report(
    checks: dict[str, Callable[[Infrastructure], Awaitable[None]]], infra: Infrastructure
) -> HealthReport:
    results = await asyncio.gather(*(_run(n, c, infra) for n, c in checks.items()))
    components = dict(zip(checks, results, strict=True))
    overall: Status = "ok" if all(c.status == "ok" for c in results) else "error"
    return HealthReport(status=overall, components=components)


async def readiness(infra: Infrastructure) -> HealthReport:
    """API can serve traffic: PostgreSQL, Redis, Qdrant, object storage."""
    return await _report(_READY_CHECKS, infra)


async def workers(infra: Infrastructure) -> HealthReport:
    return await _report({"celery_workers": _check_workers}, infra)
