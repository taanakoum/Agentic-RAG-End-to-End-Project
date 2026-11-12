"""Thumbs up/down feedback on assistant answers, scoped to the owning user."""

from __future__ import annotations

import uuid
from typing import cast

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import ResourceNotFoundError
from app.infra.rate_limit import RateLimiter, subject_key
from app.models.chat import Conversation, Message
from app.models.feedback import MessageFeedback
from app.schemas.feedback import FeedbackCreate, FeedbackPublic
from app.services.auth_service import AuthenticatedUser


class FeedbackService:
    def __init__(self, *, session: AsyncSession, settings: Settings, limiter: RateLimiter) -> None:
        self._session = session
        self._settings = settings
        self._limiter = limiter

    async def _own_assistant_message(self, user_id: uuid.UUID, message_id: uuid.UUID) -> bool:
        """Only the owner's assistant messages of live conversations can be rated.

        Anything else (someone else's message, a user message, a deleted conversation, a random
        id) is the same 404, so message ids cannot be probed.
        """
        found = await self._session.execute(
            select(Message.id)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Message.id == message_id,
                Message.user_id == user_id,
                Message.role == "assistant",
                Conversation.user_id == user_id,
                Conversation.deleted_at.is_(None),
            )
        )
        return found.scalar_one_or_none() is not None

    async def set(self, user: AuthenticatedUser, body: FeedbackCreate) -> FeedbackPublic:
        s = self._settings
        await self._limiter.hit(
            "feedback",
            subject_key(str(user.id)),
            limit=s.rl_feedback_limit,
            window_s=s.rl_feedback_window_s,
        )
        if not await self._own_assistant_message(user.id, body.message_id):
            raise ResourceNotFoundError()
        statement = insert(MessageFeedback).values(
            user_id=user.id, message_id=body.message_id, rating=body.rating, comment=body.comment
        )
        statement = statement.on_conflict_do_update(
            constraint="uq_message_feedback_user_message",
            set_={"rating": body.rating, "comment": body.comment, "updated_at": func.now()},
        ).returning(MessageFeedback.updated_at)
        updated_at = cast("object", (await self._session.execute(statement)).scalar_one())
        await self._session.commit()
        return FeedbackPublic.model_validate(
            {
                "message_id": body.message_id,
                "rating": body.rating,
                "comment": body.comment,
                "updated_at": updated_at,
            }
        )

    async def clear(self, user: AuthenticatedUser, message_id: uuid.UUID) -> None:
        """Idempotent: removing feedback that does not exist is not an error."""
        await self._session.execute(
            delete(MessageFeedback).where(
                MessageFeedback.user_id == user.id, MessageFeedback.message_id == message_id
            )
        )
        await self._session.commit()
