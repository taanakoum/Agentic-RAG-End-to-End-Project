"""Long-term memory (separate from chat history) and per-user preferences.

PostgreSQL is the source of truth; Qdrant holds a derived dense vector per memory (same point id
as the memory id) that is written/removed by the worker via the transactional outbox. A memory is
deleted in two steps like documents: tombstone (`deleted_at`, instantly invisible) -> worker removes
the vector -> worker hard-deletes the row.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base

MEMORY_KINDS = ("semantic", "episodic", "summary")
MEMORY_SOURCES = ("user", "consolidation")
INDEX_STATES = ("pending", "indexed")


class Memory(Base):
    __tablename__ = "memories"
    __table_args__ = (
        CheckConstraint(
            "kind IN (" + ", ".join(f"'{k}'" for k in MEMORY_KINDS) + ")", name="kind_valid"
        ),
        CheckConstraint(
            "source IN (" + ", ".join(f"'{s}'" for s in MEMORY_SOURCES) + ")", name="source_valid"
        ),
        CheckConstraint(
            "index_state IN (" + ", ".join(f"'{s}'" for s in INDEX_STATES) + ")",
            name="index_state_valid",
        ),
        CheckConstraint("importance BETWEEN 1 AND 5", name="importance_range"),
        # the same fact is stored once per user (live rows only)
        Index(
            "uq_memories_user_content",
            "user_id",
            "content_hash",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index("ix_memories_user_created", "user_id", "created_at"),
        Index("ix_memories_deleted", "deleted_at", postgresql_where=text("deleted_at IS NOT NULL")),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(12), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    importance: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("3"))
    source: Mapped[str] = mapped_column(String(14), nullable=False)
    source_conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True
    )
    index_state: Mapped[str] = mapped_column(
        String(10), nullable=False, server_default=text("'pending'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class UserPreferences(Base):
    """Consent and retention. Memory is OFF until the user turns it on."""

    __tablename__ = "user_preferences"
    __table_args__ = (
        CheckConstraint(
            "memory_retention_days BETWEEN 1 AND 3650", name="memory_retention_days_range"
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    memory_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    memory_retention_days: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("365")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
