"""Long-term memory for one user: CRUD, relevance search, reset, preferences (consent, retention).

Memory is NOT chat history: it holds stable facts and preferences, is created only by explicit
user action or by consent-gated consolidation, and is retrieved by relevance (never injected
wholesale). PostgreSQL is the source of truth; the Qdrant vector is derived and maintained by the
worker through the outbox (so an API crash never leaves a memory without its index entry or a
deleted memory with a live vector for long).
"""

from __future__ import annotations

import asyncio
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime

from qdrant_client import AsyncQdrantClient
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import (
    DuplicateResourceError,
    ErrorDetail,
    RequestValidationAppError,
    ResourceNotFoundError,
)
from app.infra.rate_limit import RateLimiter, subject_key
from app.ingestion.embedding import get_embedder
from app.memory.index import search_vectors
from app.memory.sensitive import sensitive_reason
from app.models.memory import Memory
from app.repositories.memory import MemoryRepository
from app.schemas.memory import (
    MemoryCreate,
    MemoryList,
    MemoryPublic,
    MemoryResetAccepted,
    MemoryUpdate,
    PreferencesPublic,
    PreferencesUpdate,
)
from app.services.auth_service import AuthenticatedUser


def to_public(memory: Memory) -> MemoryPublic:
    return MemoryPublic(
        id=memory.id,
        kind=memory.kind,  # pyright: ignore[reportArgumentType]
        content=memory.content,
        importance=int(memory.importance),
        source=memory.source,  # pyright: ignore[reportArgumentType]
        created_at=memory.created_at,
        updated_at=memory.updated_at,
        last_used_at=memory.last_used_at,
    )


def _invalid(field: str, code: str, message: str) -> RequestValidationAppError:
    return RequestValidationAppError(details=[ErrorDetail(field=field, code=code, message=message)])


@dataclass(frozen=True, slots=True)
class MemoryHit:
    memory: Memory
    score: float


class MemoryService:
    def __init__(
        self,
        *,
        session: AsyncSession,
        settings: Settings,
        limiter: RateLimiter,
        qdrant: AsyncQdrantClient,
        executor: ThreadPoolExecutor,
    ) -> None:
        self._session = session
        self._settings = settings
        self._limiter = limiter
        self._qdrant = qdrant
        self._executor = executor
        self._repo = MemoryRepository(session)

    # ------------------------------------------------------------------ preferences

    async def preferences(self, user: AuthenticatedUser) -> PreferencesPublic:
        prefs = await self._repo.get_preferences(user.id)
        return PreferencesPublic(
            memory_enabled=bool(prefs.memory_enabled) if prefs else False,
            memory_retention_days=int(prefs.memory_retention_days) if prefs else 365,
        )

    async def update_preferences(
        self, user: AuthenticatedUser, body: PreferencesUpdate
    ) -> PreferencesPublic:
        prefs = await self._repo.ensure_preferences(user.id)
        if body.memory_enabled is not None:
            prefs.memory_enabled = body.memory_enabled
        if body.memory_retention_days is not None:
            prefs.memory_retention_days = body.memory_retention_days
        prefs.updated_at = datetime.now(UTC)
        await self._session.commit()
        return PreferencesPublic(
            memory_enabled=bool(prefs.memory_enabled),
            memory_retention_days=int(prefs.memory_retention_days),
        )

    # ------------------------------------------------------------------ validation

    def _check_content(self, content: str) -> None:
        if len(content) > self._settings.memory_max_chars:
            raise _invalid(
                "content",
                "content_too_long",
                f"A memory can have at most {self._settings.memory_max_chars} characters.",
            )
        reason = sensitive_reason(content)
        if reason is not None:
            raise _invalid(
                "content",
                "sensitive_content",
                "This looks like sensitive data (credentials, payment, contact or identity "
                "details) and is not stored in memory.",
            )

    async def _rate_limit(self, user: AuthenticatedUser) -> None:
        s = self._settings
        await self._limiter.hit(
            "memory_write",
            subject_key(str(user.id)),
            limit=s.rl_memory_write_limit,
            window_s=s.rl_memory_write_window_s,
        )

    # ------------------------------------------------------------------ CRUD

    async def create(self, user: AuthenticatedUser, body: MemoryCreate) -> MemoryPublic:
        await self._rate_limit(user)
        self._check_content(body.content)
        if await self._repo.count_live(user.id) >= self._settings.memory_max_per_user:
            raise _invalid(
                "content",
                "memory_limit_reached",
                f"You can keep at most {self._settings.memory_max_per_user} memories.",
            )
        try:
            memory = await self._repo.add(
                user.id,
                kind=body.kind,
                content=body.content,
                importance=body.importance,
                source="user",
            )
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            raise DuplicateResourceError("You already saved this memory.") from exc
        return to_public(memory)

    async def list(
        self, user: AuthenticatedUser, *, kind: str | None, limit: int, offset: int
    ) -> MemoryList:
        rows, total = await self._repo.list_owned(user.id, kind=kind, limit=limit, offset=offset)
        return MemoryList(
            items=[to_public(m) for m in rows], total=total, limit=limit, offset=offset
        )

    async def update(
        self, user: AuthenticatedUser, memory_id: uuid.UUID, body: MemoryUpdate
    ) -> MemoryPublic:
        await self._rate_limit(user)
        memory = await self._repo.get_owned(user.id, memory_id, for_update=True)
        if memory is None:
            raise ResourceNotFoundError()
        reindex = False
        if body.content is not None and body.content != memory.content:
            self._check_content(body.content)
            from app.repositories.memory import content_hash

            memory.content = body.content
            memory.content_hash = content_hash(body.content)
            reindex = True
        if body.kind is not None and body.kind != memory.kind:
            memory.kind = body.kind
            reindex = True  # the kind is part of the vector payload
        if body.importance is not None:
            memory.importance = body.importance
        memory.updated_at = datetime.now(UTC)
        if reindex:
            self._repo.queue_reindex(memory)
        try:
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            raise DuplicateResourceError("You already saved this memory.") from exc
        return to_public(memory)

    async def delete(self, user: AuthenticatedUser, memory_id: uuid.UUID) -> None:
        """Tombstone now (invisible, never retrieved again); the worker removes vector and row."""
        memory = await self._repo.tombstone(user.id, memory_id, now=datetime.now(UTC))
        if memory is None:
            raise ResourceNotFoundError()
        await self._session.commit()

    async def reset(self, user: AuthenticatedUser) -> MemoryResetAccepted:
        count = await self._repo.tombstone_all(user.id, now=datetime.now(UTC))
        await self._session.commit()
        return MemoryResetAccepted(memories=count)

    # ------------------------------------------------------------------ retrieval

    async def relevant(
        self,
        user: AuthenticatedUser,
        query: str,
        *,
        k: int | None = None,
        respect_consent: bool = True,
    ) -> list[MemoryHit]:
        """The user's memories that are relevant to `query` (never "all memories").

        With `respect_consent` (the agent path) nothing is returned unless the user enabled
        memory. Vector hits are re-checked in PostgreSQL: a deleted or foreign memory can never
        be returned, even while its vector still exists.
        """
        s = self._settings
        if respect_consent:
            prefs = await self._repo.get_preferences(user.id)
            if prefs is None or not prefs.memory_enabled:
                return []
        text = query.strip()
        if not text:
            return []
        embedder = get_embedder(s)
        vector = await asyncio.get_running_loop().run_in_executor(
            self._executor, embedder.embed_query_dense, text
        )
        scored = await search_vectors(
            self._qdrant,
            s,
            user_id=user.id,
            vector=vector,
            limit=min(k or s.memory_search_k, 20),
            min_score=s.memory_min_score,
        )
        live = await self._repo.live_by_ids(user.id, [memory_id for memory_id, _ in scored])
        hits = [MemoryHit(live[mid], score) for mid, score in scored if mid in live]
        await self._repo.touch([h.memory for h in hits], now=datetime.now(UTC))
        await self._session.commit()
        return hits
