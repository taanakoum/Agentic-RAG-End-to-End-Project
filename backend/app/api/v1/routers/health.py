"""Health endpoints (unauthenticated, no sensitive data)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel

from app.dependencies.infra import get_infra
from app.infra.container import Infrastructure
from app.services import health as health_service
from app.services.health import HealthReport

router = APIRouter(prefix="/health", tags=["health"])

InfraDep = Annotated[Infrastructure, Depends(get_infra)]


class LiveResponse(BaseModel):
    status: str = "ok"


@router.get("/live", response_model=LiveResponse)
async def live() -> LiveResponse:
    """Process is up. Does not touch dependencies."""
    return LiveResponse()


@router.get("/ready", response_model=HealthReport)
async def ready(infra: InfraDep, response: Response) -> HealthReport:
    """PostgreSQL, Redis, Qdrant and storage are reachable. 503 otherwise."""
    report = await health_service.readiness(infra)
    if report.status != "ok":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return report


class LlmStatus(BaseModel):
    configured: bool
    daily_budget: int = 0  # 0 = unlimited
    remaining_today: int | None = None  # None = unlimited
    resets_in_s: int = 0
    per_minute_limit: int = 0


@router.get("/llm", response_model=LlmStatus)
async def llm(infra: InfraDep) -> LlmStatus:
    """Is a language model configured, and how much of today's free-tier budget is left?

    Never reveals the key, the model names or any user data. A budget of 0 means unlimited.
    """
    s = infra.settings
    guard = infra.llm_quota
    usage = (await guard.usage([s.llm_model]))[0] if s.llm_model else None
    return LlmStatus(
        configured=infra.llm.configured,
        daily_budget=s.llm_daily_request_budget,
        remaining_today=usage.remaining if usage else None,
        resets_in_s=guard.resets_in_s(),
        per_minute_limit=s.llm_requests_per_minute,
    )


@router.get("/workers", response_model=HealthReport)
async def workers(infra: InfraDep, response: Response) -> HealthReport:
    """At least one Celery worker answers a control ping. 503 otherwise."""
    report = await health_service.workers(infra)
    if report.status != "ok":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return report
