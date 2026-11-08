"""The life cycle of one chat turn: accept (idempotently), run, complete or fail.

Same-thread concurrency policy (ADR-023), chosen from the options in docs/spec/05:
  * A PostgreSQL row lock on the conversation serialises everything that appends to it, so
    message sequence numbers are gapless and ordered, and two requests cannot both start.
  * At most one execution per conversation is `running` (partial unique index = the safety net).
    A second message while one is running is REJECTED with 409 CONCURRENCY_CONFLICT; nothing is
    queued, so a slow generation cannot pile up hidden work. Different conversations (even of the
    same user) never contend: the lock is per conversation row.
  * Every execution holds a LEASE. If a worker dies, the next request after the lease expired
    marks the dead execution `failed` and proceeds; a late `complete` from the dead worker is
    refused (status is no longer `running`), so a checkpoint or answer can never be overwritten
    by a stale writer.
  * The client's idempotency key is unique per conversation. Replaying a key returns the
    original execution (and its answer once completed); reusing it with a different body is an
    error.
Lock order is always conversation row -> execution row.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import (
    ConcurrencyConflictError,
    ErrorDetail,
    IdempotencyKeyReusedError,
    RequestValidationAppError,
    ResourceNotFoundError,
)
from app.core.logging import get_logger
from app.models.chat import ChatExecution, Message
from app.repositories.conversations import ConversationRepository

log = get_logger(__name__)

DEFAULT_TITLE = "New conversation"
_KEY = re.compile(r"^[A-Za-z0-9_.:-]{8,128}$")


def _now() -> datetime:
    return datetime.now(UTC)


def request_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class TurnHandle:
    execution_id: uuid.UUID
    conversation_id: uuid.UUID
    thread_id: uuid.UUID
    user_message_id: uuid.UUID
    user_message_seq: int
    status: str
    replayed: bool
    assistant_message: Message | None = None


def _validation(field: str, code: str, message: str) -> RequestValidationAppError:
    return RequestValidationAppError(details=[ErrorDetail(field=field, code=code, message=message)])


class ChatTurnService:
    def __init__(
        self,
        *,
        session: AsyncSession,
        settings: Settings,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self._session = session
        self._settings = settings
        self._repo = ConversationRepository(session)
        self._clock = clock

    # ------------------------------------------------------------------ begin

    async def begin(
        self,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        *,
        idempotency_key: str,
        content: str,
    ) -> TurnHandle:
        s = self._settings
        if not _KEY.match(idempotency_key):
            raise _validation(
                "idempotency_key",
                "invalid_idempotency_key",
                "Use 8-128 characters from A-Z a-z 0-9 _ . : -",
            )
        text = content.strip()
        if not text:
            raise _validation("message", "message_empty", "Enter a message.")
        if len(text) > s.chat_max_message_chars:
            raise _validation(
                "message", "message_too_long", f"At most {s.chat_max_message_chars} characters."
            )
        digest = request_hash(text)
        now = self._clock()

        conversation = await self._repo.get_owned(user_id, conversation_id, for_update=True)
        if conversation is None:
            raise ResourceNotFoundError()

        existing = await self._repo.get_execution_by_key(conversation_id, idempotency_key)
        if existing is not None:
            if existing.request_hash != digest:
                raise IdempotencyKeyReusedError()
            await self._session.commit()  # release the row lock: a replay changes nothing
            return await self._handle_for(existing, replayed=True)

        running = await self._repo.get_running_execution(conversation_id)
        if running is not None:
            if running.lease_expires_at > now:
                raise ConcurrencyConflictError(
                    "Another message in this conversation is still being answered."
                )
            running.status = "failed"
            running.error_code = "LEASE_EXPIRED"
            running.finished_at = now
            log.warning("chat_execution_lease_expired", execution_id=str(running.id))
            await self._session.flush()

        if conversation.last_seq == 0 and conversation.title == DEFAULT_TITLE:
            conversation.title = " ".join(text.split())[:60]
        conversation.last_seq += 1
        user_message = Message(
            id=uuid.uuid4(),
            conversation_id=conversation_id,
            user_id=user_id,
            seq=conversation.last_seq,
            role="user",
            content=text,
        )
        execution = ChatExecution(
            id=uuid.uuid4(),
            conversation_id=conversation_id,
            user_id=user_id,
            idempotency_key=idempotency_key,
            request_hash=digest,
            status="running",
            user_message_id=user_message.id,
            lease_expires_at=now + timedelta(seconds=s.chat_lease_s),
        )
        user_message.execution_id = execution.id
        conversation.updated_at = now
        self._session.add_all([user_message, execution])
        try:
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            # the partial unique indexes are the last line of defence against a lost race
            raise ConcurrencyConflictError(
                "Another message in this conversation is still being answered."
            ) from exc
        return TurnHandle(
            execution_id=execution.id,
            conversation_id=conversation_id,
            thread_id=conversation.thread_id,
            user_message_id=user_message.id,
            user_message_seq=user_message.seq,
            status="running",
            replayed=False,
        )

    async def _handle_for(self, execution: ChatExecution, *, replayed: bool) -> TurnHandle:
        conversation_row = await self._repo.get_owned(execution.user_id, execution.conversation_id)
        if conversation_row is None:
            raise ResourceNotFoundError()
        user_message = await self._repo.get_message(execution.user_message_id)
        assistant = (
            await self._repo.get_message(execution.assistant_message_id)
            if execution.assistant_message_id
            else None
        )
        return TurnHandle(
            execution_id=execution.id,
            conversation_id=execution.conversation_id,
            thread_id=conversation_row.thread_id,
            user_message_id=execution.user_message_id,
            user_message_seq=user_message.seq if user_message else 0,
            status=execution.status,
            replayed=replayed,
            assistant_message=assistant,
        )

    # ------------------------------------------------------------------ finish

    async def complete(
        self,
        user_id: uuid.UUID,
        execution_id: uuid.UUID,
        *,
        answer: str,
        response: dict[str, Any] | None,
    ) -> Message:
        """Persist the answer and finish the execution atomically (or refuse).

        Refused with ConcurrencyConflictError when the execution is no longer `running`
        (cancelled by deletion, or reaped after its lease expired).
        """
        execution = await self._repo.get_execution_owned(user_id, execution_id)
        if execution is None:
            raise ResourceNotFoundError()
        conversation = await self._repo.get_owned(
            user_id, execution.conversation_id, for_update=True
        )
        if conversation is None:
            raise ConcurrencyConflictError("The conversation was deleted while it was answering.")
        locked = await self._repo.get_execution_owned(user_id, execution_id, for_update=True)
        if locked is None or locked.status != "running":
            raise ConcurrencyConflictError("This execution is no longer running.")
        conversation.last_seq += 1
        now = self._clock()
        assistant = Message(
            id=uuid.uuid4(),
            conversation_id=conversation.id,
            user_id=user_id,
            seq=conversation.last_seq,
            role="assistant",
            content=answer,
            response=response,
            execution_id=locked.id,
        )
        locked.status = "completed"
        locked.assistant_message_id = assistant.id
        locked.finished_at = now
        conversation.updated_at = now
        self._session.add(assistant)
        await self._session.commit()
        return assistant

    async def fail(self, user_id: uuid.UUID, execution_id: uuid.UUID, *, error_code: str) -> bool:
        """Mark a running execution failed. Returns False if it was not running (already final)."""
        execution = await self._repo.get_execution_owned(user_id, execution_id)
        if execution is None:
            return False
        await self._repo.get_owned(user_id, execution.conversation_id, for_update=True)
        result = await self._session.execute(
            update(ChatExecution)
            .where(ChatExecution.id == execution_id, ChatExecution.status == "running")
            .values(status="failed", error_code=error_code[:64], finished_at=self._clock())
        )
        await self._session.commit()
        return bool(getattr(result, "rowcount", 0))

    async def renew_lease(self, user_id: uuid.UUID, execution_id: uuid.UUID) -> None:
        """Extend the lease of a running execution (long generations); refuse if it was lost."""
        result = await self._session.execute(
            update(ChatExecution)
            .where(
                ChatExecution.id == execution_id,
                ChatExecution.user_id == user_id,
                ChatExecution.status == "running",
            )
            .values(lease_expires_at=self._clock() + timedelta(seconds=self._settings.chat_lease_s))
        )
        await self._session.commit()
        if not getattr(result, "rowcount", 0):
            raise ConcurrencyConflictError("This execution is no longer running.")
