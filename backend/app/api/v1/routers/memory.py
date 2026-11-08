"""Memory endpoints. HTTP only; logic lives in MemoryService."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Query, Response, status

from app.core.exceptions import error_responses
from app.dependencies.auth import InfraDep, MemoryServiceDep, VerifiedUser
from app.schemas.memory import (
    MemoryCreate,
    MemoryHitPublic,
    MemoryList,
    MemoryPublic,
    MemoryResetAccepted,
    MemoryResetRequest,
    MemorySearchRequest,
    MemorySearchResponse,
    MemoryUpdate,
)
from app.services.background import kick_dispatcher
from app.services.memory_service import to_public

router = APIRouter(prefix="/memory", tags=["memory"])


@router.get("", response_model=MemoryList, responses=error_responses(401, 403, 422))
async def list_memories(
    current: VerifiedUser,
    service: MemoryServiceDep,
    kind: Annotated[str | None, Query(pattern="^(semantic|episodic|summary)$")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> MemoryList:
    return await service.list(current, kind=kind, limit=limit, offset=offset)


@router.post(
    "",
    response_model=MemoryPublic,
    status_code=status.HTTP_201_CREATED,
    responses=error_responses(401, 403, 409, 422, 429, 503),
)
async def create_memory(
    body: MemoryCreate,
    current: VerifiedUser,
    service: MemoryServiceDep,
    infra: InfraDep,
    background: BackgroundTasks,
) -> MemoryPublic:
    created = await service.create(current, body)
    background.add_task(kick_dispatcher, infra)
    return created


@router.post(
    "/search",
    response_model=MemorySearchResponse,
    responses=error_responses(401, 403, 422, 503),
)
async def search_memories(
    body: MemorySearchRequest, current: VerifiedUser, service: MemoryServiceDep
) -> MemorySearchResponse:
    """The memories relevant to a query (what the agent would see). Ignores the consent switch."""
    hits = await service.relevant(current, body.query, k=body.k, respect_consent=False)
    return MemorySearchResponse(
        results=[MemoryHitPublic(memory=to_public(h.memory), score=h.score) for h in hits]
    )


@router.post(
    "/reset",
    response_model=MemoryResetAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    responses=error_responses(401, 403, 422),
)
async def reset_memory(
    body: MemoryResetRequest,
    current: VerifiedUser,
    service: MemoryServiceDep,
    infra: InfraDep,
    background: BackgroundTasks,
) -> MemoryResetAccepted:
    result = await service.reset(current)
    background.add_task(kick_dispatcher, infra)
    return result


@router.patch(
    "/{memory_id}",
    response_model=MemoryPublic,
    responses=error_responses(401, 403, 404, 409, 422, 429),
)
async def update_memory(
    memory_id: uuid.UUID,
    body: MemoryUpdate,
    current: VerifiedUser,
    service: MemoryServiceDep,
    infra: InfraDep,
    background: BackgroundTasks,
) -> MemoryPublic:
    updated = await service.update(current, memory_id, body)
    background.add_task(kick_dispatcher, infra)
    return updated


@router.delete(
    "/{memory_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=error_responses(401, 403, 404, 422),
)
async def delete_memory(
    memory_id: uuid.UUID,
    current: VerifiedUser,
    service: MemoryServiceDep,
    infra: InfraDep,
    background: BackgroundTasks,
) -> Response:
    await service.delete(current, memory_id)
    background.add_task(kick_dispatcher, infra)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
