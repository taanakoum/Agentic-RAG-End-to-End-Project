"""Conversation, message and execution persistence. Every query is scoped by the owning user."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.ingestion.outbox import KIND_PURGE_CONVERSATION
from app.models.chat import ChatExecution, Conversation, Message
from app.models.document import OutboxEvent


class ConversationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ------------------------------------------------------------------ conversations

    async def create(
        self, user_id: uuid.UUID, *, title: str, agent_config: dict[str, Any]
    ) -> Conversation:
        conversation = Conversation(
            id=uuid.uuid4(),
            user_id=user_id,
            thread_id=uuid.uuid4(),
            title=title,
            agent_config=agent_config,
            last_seq=0,
        )
        self._session.add(conversation)
        await self._session.flush()
        await self._session.refresh(conversation)
        return conversation

    async def get_owned(
        self, user_id: uuid.UUID, conversation_id: uuid.UUID, *, for_update: bool = False
    ) -> Conversation | None:
        """The caller's live conversation. Foreign, deleted and unknown ids are all None.

        `for_update` takes the row lock that serialises everything that appends to this
        conversation (message sequence numbers, execution acquisition).
        """
        stmt = select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.user_id == user_id,
            Conversation.deleted_at.is_(None),
        )
        if for_update:
            stmt = stmt.with_for_update()
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_owned(
        self, user_id: uuid.UUID, *, limit: int, offset: int
    ) -> tuple[list[Conversation], int]:
        window = func.count().over().label("total")
        rows = (
            await self._session.execute(
                select(Conversation, window)
                .where(Conversation.user_id == user_id, Conversation.deleted_at.is_(None))
                .order_by(Conversation.updated_at.desc(), Conversation.id)
                .limit(limit)
                .offset(offset)
            )
        ).all()
        if rows:
            return [row[0] for row in rows], int(rows[0][1])
        total = (
            await self._session.execute(
                select(func.count())
                .select_from(Conversation)
                .where(Conversation.user_id == user_id, Conversation.deleted_at.is_(None))
            )
        ).scalar_one()
        return [], int(total)

    async def tombstone(
        self, user_id: uuid.UUID, conversation_id: uuid.UUID, *, now: datetime
    ) -> Conversation | None:
        """Hide the conversation, cancel its running execution and queue the purge (one tx)."""
        conversation = await self.get_owned(user_id, conversation_id, for_update=True)
        if conversation is None:
            return None
        conversation.deleted_at = now
        await self._session.execute(
            update(ChatExecution)
            .where(
                ChatExecution.conversation_id == conversation_id, ChatExecution.status == "running"
            )
            .values(status="cancelled", error_code="CONVERSATION_DELETED", finished_at=now)
        )
        self._session.add(
            OutboxEvent(
                kind=KIND_PURGE_CONVERSATION,
                payload={"conversation_id": str(conversation_id), "user_id": str(user_id)},
            )
        )
        return conversation

    # ------------------------------------------------------------------ messages

    async def message_count(self, conversation_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
        if not conversation_ids:
            return {}
        rows = await self._session.execute(
            select(Conversation.id, Conversation.last_seq).where(
                Conversation.id.in_(conversation_ids)
            )
        )
        return {row.id: int(row.last_seq) for row in rows}

    async def list_messages(
        self,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        *,
        after_seq: int,
        limit: int,
    ) -> tuple[list[Message], bool]:
        rows = (
            (
                await self._session.execute(
                    select(Message)
                    .where(
                        Message.conversation_id == conversation_id,
                        Message.user_id == user_id,
                        Message.seq > after_seq,
                    )
                    .order_by(Message.seq)
                    .limit(limit + 1)
                )
            )
            .scalars()
            .all()
        )
        return list(rows[:limit]), len(rows) > limit

    async def recent_messages(
        self, user_id: uuid.UUID, conversation_id: uuid.UUID, *, limit: int
    ) -> list[Message]:
        """The last `limit` messages in chronological order (context for the next turn)."""
        rows = (
            (
                await self._session.execute(
                    select(Message)
                    .where(Message.conversation_id == conversation_id, Message.user_id == user_id)
                    .order_by(Message.seq.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return list(reversed(rows))

    async def recent_exchanges(
        self, user_id: uuid.UUID, conversation_id: uuid.UUID, *, limit: int
    ) -> list[Message]:
        """The last `limit` messages that belong to COMPLETED turns, oldest first.

        A running, failed or cancelled turn leaves only its user message behind; it must not
        become context for the next answer.
        """
        rows = (
            (
                await self._session.execute(
                    select(Message)
                    .join(ChatExecution, Message.execution_id == ChatExecution.id)
                    .where(
                        Message.conversation_id == conversation_id,
                        Message.user_id == user_id,
                        ChatExecution.status == "completed",
                    )
                    .order_by(Message.seq.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return list(reversed(rows))

    # ------------------------------------------------------------------ executions

    async def get_execution_by_key(
        self, conversation_id: uuid.UUID, key: str
    ) -> ChatExecution | None:
        return (
            await self._session.execute(
                select(ChatExecution).where(
                    ChatExecution.conversation_id == conversation_id,
                    ChatExecution.idempotency_key == key,
                )
            )
        ).scalar_one_or_none()

    async def get_running_execution(self, conversation_id: uuid.UUID) -> ChatExecution | None:
        return (
            await self._session.execute(
                select(ChatExecution).where(
                    ChatExecution.conversation_id == conversation_id,
                    ChatExecution.status == "running",
                )
            )
        ).scalar_one_or_none()

    async def get_execution_owned(
        self, user_id: uuid.UUID, execution_id: uuid.UUID, *, for_update: bool = False
    ) -> ChatExecution | None:
        stmt = select(ChatExecution).where(
            ChatExecution.id == execution_id, ChatExecution.user_id == user_id
        )
        if for_update:
            # populate_existing: the row may already be in the session from an earlier unlocked
            # read; after waiting for the lock we must see the CURRENT committed state.
            stmt = stmt.with_for_update().execution_options(populate_existing=True)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_message(self, message_id: uuid.UUID) -> Message | None:
        return (
            await self._session.execute(select(Message).where(Message.id == message_id))
        ).scalar_one_or_none()
