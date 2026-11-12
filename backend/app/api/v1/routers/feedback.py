"""Feedback endpoints. HTTP only; logic lives in FeedbackService."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, status

from app.core.exceptions import error_responses
from app.dependencies.auth import FeedbackServiceDep, VerifiedUser
from app.schemas.feedback import FeedbackCreate, FeedbackPublic

router = APIRouter(prefix="/feedback", tags=["feedback"])


@router.post("", response_model=FeedbackPublic, responses=error_responses(401, 403, 404, 422, 429))
async def set_feedback(
    body: FeedbackCreate, current: VerifiedUser, service: FeedbackServiceDep
) -> FeedbackPublic:
    return await service.set(current, body)


@router.delete(
    "/{message_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=error_responses(401, 403, 422),
)
async def clear_feedback(
    message_id: uuid.UUID, current: VerifiedUser, service: FeedbackServiceDep
) -> None:
    await service.clear(current, message_id)
