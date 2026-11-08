"""Async memory persistence for the API. Every query is scoped by the owning user."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from app.ingestion.outbox import KIND_INDEX_MEMORY, KIND_PURGE_MEMORY, KIND_PURGE_USER_MEMORIES
from app.models.document import OutboxEvent
from app.models.memory import Memory, UserPreferences


def content_hash(content: str) -> str:
    """Stable identity of a memory's meaning-bearing text (case and spacing insensitive)."""
    normalized = " ".join(content.lower().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


class MemoryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ------------------------------------------------------------------ preferences

    async def get_preferences(self, user_id: uuid.UUID) -> UserPreferences | None:
        return (
            await self._session.execute(
                select(UserPreferences).where(UserPreferences.user_id == user_id)
            )
        ).scalar_one_or_none()

    async def ensure_preferences(self, user_id: uuid.UUID) -> UserPreferences:
        existing = await self.get_preferences(user_id)
        if existing is not None:
            return existing
        prefs = UserPreferences(user_id=user_id, memory_enabled=False, memory_retention_days=365)
        self._session.add(prefs)
        await self._session.flush()
        return prefs

    # ------------------------------------------------------------------ memories

    async def count_live(self, user_id: uuid.UUID) -> int:
        return int(
            (
                await self._session.execute(
                    select(func.count())
                    .select_from(Memory)
                    .where(Memory.user_id == user_id, Memory.deleted_at.is_(None))
                )
            ).scalar_one()
        )

    async def add(
        self,
        user_id: uuid.UUID,
        *,
        kind: str,
        content: str,
        importance: int,
        source: str,
        source_conversation_id: uuid.UUID | None = None,
    ) -> Memory:
        memory = Memory(
            id=uuid.uuid4(),
            user_id=user_id,
            kind=kind,
            content=content,
            content_hash=content_hash(content),
            importance=importance,
            source=source,
            source_conversation_id=source_conversation_id,
            index_state="pending",
        )
        self._session.add(memory)
        self._session.add(
            OutboxEvent(
                kind=KIND_INDEX_MEMORY,
                payload={"memory_id": str(memory.id), "user_id": str(user_id)},
            )
        )
        await self._session.flush()
        return memory

    async def get_owned(
        self, user_id: uuid.UUID, memory_id: uuid.UUID, *, for_update: bool = False
    ) -> Memory | None:
        stmt = select(Memory).where(
            Memory.id == memory_id, Memory.user_id == user_id, Memory.deleted_at.is_(None)
        )
        if for_update:
            stmt = stmt.with_for_update().execution_options(populate_existing=True)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_owned(
        self, user_id: uuid.UUID, *, kind: str | None, limit: int, offset: int
    ) -> tuple[list[Memory], int]:
        conditions: list[Any] = [Memory.user_id == user_id, Memory.deleted_at.is_(None)]
        if kind is not None:
            conditions.append(Memory.kind == kind)
        total = int(
            (
                await self._session.execute(
                    select(func.count()).select_from(Memory).where(*conditions)
                )
            ).scalar_one()
        )
        rows = (
            (
                await self._session.execute(
                    select(Memory)
                    .where(*conditions)
                    .order_by(Memory.created_at.desc(), Memory.id)
                    .limit(limit)
                    .offset(offset)
                )
            )
            .scalars()
            .all()
        )
        return list(rows), total

    async def live_by_ids(
        self, user_id: uuid.UUID, ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, Memory]:
        if not ids:
            return {}
        rows = await self._session.execute(
            select(Memory).where(
                Memory.user_id == user_id, Memory.id.in_(list(ids)), Memory.deleted_at.is_(None)
            )
        )
        return {m.id: m for m in rows.scalars()}

    async def touch(self, memories: Sequence[Memory], *, now: datetime) -> None:
        """Record that these memories were retrieved (does NOT count as an edit)."""
        if not memories:
            return
        await self._session.execute(
            update(Memory)
            .where(Memory.id.in_([m.id for m in memories]))
            # keeping updated_at explicit stops the column's onupdate from firing and expiring it
            .values(last_used_at=now, updated_at=Memory.updated_at)
            .execution_options(synchronize_session=False)
        )
        for memory in memories:
            set_committed_value(memory, "last_used_at", now)

    def queue_reindex(self, memory: Memory) -> None:
        memory.index_state = "pending"
        self._session.add(
            OutboxEvent(
                kind=KIND_INDEX_MEMORY,
                payload={"memory_id": str(memory.id), "user_id": str(memory.user_id)},
            )
        )

    async def tombstone(
        self, user_id: uuid.UUID, memory_id: uuid.UUID, *, now: datetime
    ) -> Memory | None:
        memory = await self.get_owned(user_id, memory_id, for_update=True)
        if memory is None:
            return None
        memory.deleted_at = now
        self._session.add(
            OutboxEvent(
                kind=KIND_PURGE_MEMORY,
                payload={"memory_id": str(memory_id), "user_id": str(user_id)},
            )
        )
        return memory

    async def tombstone_all(self, user_id: uuid.UUID, *, now: datetime) -> int:
        result = await self._session.execute(
            update(Memory)
            .where(Memory.user_id == user_id, Memory.deleted_at.is_(None))
            .values(deleted_at=now)
        )
        self._session.add(
            OutboxEvent(kind=KIND_PURGE_USER_MEMORIES, payload={"user_id": str(user_id)})
        )
        return int(getattr(result, "rowcount", 0) or 0)
