"""Session CRUD for one user. Chat turns live in ChatTurnService."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import ResourceNotFoundError
from app.infra.rate_limit import RateLimiter, subject_key
from app.models.chat import Conversation, Message
from app.repositories.conversations import ConversationRepository
from app.schemas.chat import (
    AgentConfig,
    ConversationCreate,
    ConversationList,
    ConversationPublic,
    ConversationUpdate,
    MessageList,
    MessagePublic,
)
from app.schemas.feedback import MessageFeedbackSummary
from app.services.auth_service import AuthenticatedUser
from app.services.chat_turns import DEFAULT_TITLE


def to_public(conversation: Conversation) -> ConversationPublic:
    return ConversationPublic(
        id=conversation.id,
        title=conversation.title,
        agent_config=AgentConfig.model_validate(conversation.agent_config),
        message_count=int(conversation.last_seq),
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
    )


def to_message_public(
    message: Message, feedback: tuple[str, str | None] | None = None
) -> MessagePublic:
    return MessagePublic(
        id=message.id,
        seq=int(message.seq),
        role=message.role,  # pyright: ignore[reportArgumentType]
        content=message.content,
        response=message.response,
        execution_id=message.execution_id,
        created_at=message.created_at,
        feedback=(
            MessageFeedbackSummary(rating=feedback[0], comment=feedback[1])  # pyright: ignore[reportArgumentType]
            if feedback
            else None
        ),
    )


class ConversationService:
    def __init__(self, *, session: AsyncSession, settings: Settings, limiter: RateLimiter) -> None:
        self._session = session
        self._settings = settings
        self._limiter = limiter
        self._repo = ConversationRepository(session)

    async def create(self, user: AuthenticatedUser, body: ConversationCreate) -> ConversationPublic:
        s = self._settings
        await self._limiter.hit(
            "session_create",
            subject_key(str(user.id)),
            limit=s.rl_session_create_limit,
            window_s=s.rl_session_create_window_s,
        )
        conversation = await self._repo.create(
            user.id,
            title=body.title or DEFAULT_TITLE,
            agent_config=body.agent_config.model_dump(mode="json"),
        )
        await self._session.commit()
        return to_public(conversation)

    async def list(self, user: AuthenticatedUser, *, limit: int, offset: int) -> ConversationList:
        items, total = await self._repo.list_owned(user.id, limit=limit, offset=offset)
        return ConversationList(
            items=[to_public(c) for c in items], total=total, limit=limit, offset=offset
        )

    async def get(self, user: AuthenticatedUser, conversation_id: uuid.UUID) -> ConversationPublic:
        conversation = await self._repo.get_owned(user.id, conversation_id)
        if conversation is None:
            raise ResourceNotFoundError()
        return to_public(conversation)

    async def update(
        self, user: AuthenticatedUser, conversation_id: uuid.UUID, body: ConversationUpdate
    ) -> ConversationPublic:
        conversation = await self._repo.get_owned(user.id, conversation_id, for_update=True)
        if conversation is None:
            raise ResourceNotFoundError()
        if body.title is not None:
            conversation.title = body.title
        if body.agent_config is not None:
            conversation.agent_config = body.agent_config.model_dump(mode="json")
        conversation.updated_at = datetime.now(UTC)
        await self._session.commit()
        return to_public(conversation)

    async def delete(self, user: AuthenticatedUser, conversation_id: uuid.UUID) -> None:
        """Tombstone now (invisible, running answer cancelled); the worker purges the rest."""
        conversation = await self._repo.tombstone(user.id, conversation_id, now=datetime.now(UTC))
        if conversation is None:
            raise ResourceNotFoundError()
        await self._session.commit()

    async def messages(
        self, user: AuthenticatedUser, conversation_id: uuid.UUID, *, after_seq: int, limit: int
    ) -> MessageList:
        if await self._repo.get_owned(user.id, conversation_id) is None:
            raise ResourceNotFoundError()
        rows, more = await self._repo.list_messages(
            user.id, conversation_id, after_seq=after_seq, limit=limit
        )
        ratings = await self._repo.feedback_for(user.id, [m.id for m in rows])
        return MessageList(
            items=[to_message_public(m, ratings.get(m.id)) for m in rows], has_more=more
        )
