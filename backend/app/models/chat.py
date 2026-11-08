"""Conversations, messages and chat executions (sessions in the spec's wording).

Identifier mapping (docs/spec/05 "Sessions and checkpoints"):
  user_id  ->  conversation.id (= the API's session_id)
           ->  conversation.thread_id (the LangGraph thread; checkpoints are keyed by it)
           ->  chat_executions.id (= execution_id; one per accepted chat request)
           ->  messages.id (user message and assistant message of that execution)

Concurrency (ADR-023): `conversations.last_seq` is the per-conversation message counter, advanced
under the conversation row lock; `uq_messages_conversation_seq` makes a duplicate or out-of-order
sequence impossible even if application code is wrong. At most ONE execution per conversation can
be `running` (partial unique index) and each client idempotency key is unique per conversation.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base

MESSAGE_ROLES = ("user", "assistant", "system")
EXECUTION_STATUSES = ("running", "completed", "failed", "cancelled")


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )


def _ts() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (
        UniqueConstraint("thread_id"),
        Index(
            "ix_conversations_user_updated",
            "user_id",
            text("updated_at DESC"),
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    thread_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    # safe, user-selectable agent options only (mode, top_k, rerank); validated by the API schema
    agent_config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    last_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    # highest message seq already examined by memory consolidation
    memory_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        CheckConstraint(
            "role IN (" + ", ".join(f"'{r}'" for r in MESSAGE_ROLES) + ")", name="role_valid"
        ),
        UniqueConstraint("conversation_id", "seq", name="uq_messages_conversation_seq"),
        Index("ix_messages_user", "user_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    # denormalised owner: every message read can be scoped by user without a join
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    role: Mapped[str] = mapped_column(String(12), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # the validated RAGResponse (assistant messages only)
    response: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    execution_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = _ts()


class ChatExecution(Base):
    __tablename__ = "chat_executions"
    __table_args__ = (
        CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in EXECUTION_STATUSES) + ")",
            name="status_valid",
        ),
        UniqueConstraint(
            "conversation_id", "idempotency_key", name="uq_chat_executions_conversation_key"
        ),
        # the database, not application code, guarantees one active execution per conversation
        Index(
            "uq_chat_executions_one_running",
            "conversation_id",
            unique=True,
            postgresql_where=text("status = 'running'"),
        ),
        Index(
            "ix_chat_executions_running_lease",
            "lease_expires_at",
            postgresql_where=text("status = 'running'"),
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    # SHA-256 of the request content: the same key with a different body is a client bug
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False)
    user_message_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    assistant_message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = _ts()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
