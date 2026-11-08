"""Ingestion job status and retry endpoints."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, status

from app.core.exceptions import error_responses
from app.dependencies.auth import CurrentUser, DocumentServiceDep
from app.schemas.documents import JobAccepted, JobPublic

router = APIRouter(prefix="/ingestion", tags=["ingestion"])


@router.get("/{job_id}", response_model=JobPublic, responses=error_responses(401, 404))
async def get_job(
    job_id: uuid.UUID, current: CurrentUser, service: DocumentServiceDep
) -> JobPublic:
    return await service.get_job(current, job_id)


@router.post(
    "/{job_id}/retry",
    response_model=JobAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    responses=error_responses(401, 404, 409),
)
async def retry_job(
    job_id: uuid.UUID, current: CurrentUser, service: DocumentServiceDep
) -> JobAccepted:
    return await service.retry(current, job_id)
