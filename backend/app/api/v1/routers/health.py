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


@router.get("/workers", response_model=HealthReport)
async def workers(infra: InfraDep, response: Response) -> HealthReport:
    """At least one Celery worker answers a control ping. 503 otherwise."""
    report = await health_service.workers(infra)
    if report.status != "ok":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return report
