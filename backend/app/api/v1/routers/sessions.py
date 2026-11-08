"""Session (conversation) endpoints. HTTP only; logic lives in ConversationService."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Query, status

from app.core.exceptions import error_responses
from app.dependencies.auth import ConversationServiceDep, InfraDep, VerifiedUser
from app.schemas.chat import (
    ConversationCreate,
    ConversationList,
    ConversationPublic,
    ConversationUpdate,
    DeleteConversationAccepted,
    MessageList,
)
from app.services.background import kick_dispatcher

router = APIRouter(prefix="/sessions", tags=["sessions"])


@router.post(
    "",
    response_model=ConversationPublic,
    status_code=status.HTTP_201_CREATED,
    responses=error_responses(401, 403, 422, 429, 503),
)
async def create_session(
    body: ConversationCreate, current: VerifiedUser, service: ConversationServiceDep
) -> ConversationPublic:
    return await service.create(current, body)


@router.get("", response_model=ConversationList, responses=error_responses(401, 403))
async def list_sessions(
    current: VerifiedUser,
    service: ConversationServiceDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ConversationList:
    return await service.list(current, limit=limit, offset=offset)


@router.get(
    "/{session_id}",
    response_model=ConversationPublic,
    responses=error_responses(401, 403, 404, 422),
)
async def get_session(
    session_id: uuid.UUID, current: VerifiedUser, service: ConversationServiceDep
) -> ConversationPublic:
    return await service.get(current, session_id)


@router.patch(
    "/{session_id}",
    response_model=ConversationPublic,
    responses=error_responses(401, 403, 404, 422),
)
async def update_session(
    session_id: uuid.UUID,
    body: ConversationUpdate,
    current: VerifiedUser,
    service: ConversationServiceDep,
) -> ConversationPublic:
    return await service.update(current, session_id, body)


@router.delete(
    "/{session_id}",
    response_model=DeleteConversationAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    responses=error_responses(401, 403, 404, 422),
)
async def delete_session(
    session_id: uuid.UUID,
    current: VerifiedUser,
    service: ConversationServiceDep,
    infra: InfraDep,
    background: BackgroundTasks,
) -> DeleteConversationAccepted:
    await service.delete(current, session_id)
    background.add_task(kick_dispatcher, infra)  # publish the purge now (beat is the fallback)
    return DeleteConversationAccepted(conversation_id=session_id)


@router.get(
    "/{session_id}/messages",
    response_model=MessageList,
    responses=error_responses(401, 403, 404, 422),
)
async def list_messages(
    session_id: uuid.UUID,
    current: VerifiedUser,
    service: ConversationServiceDep,
    after_seq: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> MessageList:
    return await service.messages(current, session_id, after_seq=after_seq, limit=limit)
