"""Worker-side memory operations (sync): index, purge, consolidate, retention.

All operations are idempotent and safe to repeat: Celery may deliver a message twice and the
reconciler re-publishes lost ones. The vector store is derived state; PostgreSQL decides.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.ingestion.outbox import KIND_PURGE_MEMORY
from app.ingestion.runtime import WorkerRuntime
from app.memory.extract import Candidate, ConversationTurn, MemoryExtractor, RuleExtractor
from app.memory.index import delete_user_vectors, delete_vector, upsert_vector
from app.memory.sensitive import sensitive_reason
from app.models.chat import Conversation, Message
from app.models.document import OutboxEvent
from app.models.memory import Memory, UserPreferences
from app.repositories.memory import content_hash

log = get_logger(__name__)

CONSOLIDATION_BATCH = 200


def _now() -> datetime:
    return datetime.now(UTC)


def index_memory(rt: WorkerRuntime, memory_id: uuid.UUID, user_id: uuid.UUID) -> str:
    with rt.session_factory() as session:
        memory = session.get(Memory, memory_id)
        if memory is None:
            delete_vector(rt.qdrant, rt.settings, user_id=user_id, memory_id=memory_id)
            return "gone"
        if memory.user_id != user_id:
            log.error("memory_ownership_mismatch", memory_id=str(memory_id))
            return "ownership_mismatch"
        if memory.deleted_at is not None:
            delete_vector(rt.qdrant, rt.settings, user_id=user_id, memory_id=memory_id)
            return "deleted"
        seen_hash, content, kind = memory.content_hash, memory.content, memory.kind
        vector = rt.embedder.embed_dense([content])[0]
        upsert_vector(
            rt.qdrant,
            rt.settings,
            memory_id=memory_id,
            user_id=user_id,
            kind=kind,
            vector=vector,
            embedding_model=rt.embedder.model_name,
        )
        # compare-and-set: if the text changed meanwhile, a newer index event will redo it
        session.execute(
            update(Memory)
            .where(Memory.id == memory_id, Memory.content_hash == seen_hash, Memory.kind == kind)
            # keep updated_at: background indexing is not an edit and must not reset retention
            .values(index_state="indexed", updated_at=Memory.updated_at)
        )
        session.commit()
    return "indexed"


def purge_memory(rt: WorkerRuntime, memory_id: uuid.UUID, user_id: uuid.UUID) -> str:
    """Vector first, row last: a failure leaves the tombstone for the reconciler to retry."""
    with rt.session_factory() as session:
        memory = session.get(Memory, memory_id)
        if memory is not None and memory.user_id != user_id:
            log.error("memory_ownership_mismatch", memory_id=str(memory_id))
            return "ownership_mismatch"
        if memory is not None and memory.deleted_at is None:
            return "not_deleted"
        delete_vector(rt.qdrant, rt.settings, user_id=user_id, memory_id=memory_id)
        if memory is None:
            return "already_purged"
        session.delete(memory)
        session.commit()
    return "purged"


def purge_user_memories(rt: WorkerRuntime, user_id: uuid.UUID) -> str:
    """Memory reset: remove every vector of the user, then every tombstoned row."""
    with rt.session_factory() as session:
        delete_user_vectors(rt.qdrant, rt.settings, user_id=user_id)
        session.execute(
            delete(Memory).where(Memory.user_id == user_id, Memory.deleted_at.is_not(None))
        )
        session.commit()
        live = session.execute(
            select(func.count())
            .select_from(Memory)
            .where(Memory.user_id == user_id, Memory.deleted_at.is_(None))
        ).scalar_one()
    if live:
        # memories created after the reset request: their vectors were just removed, re-index them
        with rt.session_factory() as session:
            for (memory_id,) in session.execute(
                select(Memory.id).where(Memory.user_id == user_id, Memory.deleted_at.is_(None))
            ).all():
                session.execute(
                    update(Memory)
                    .where(Memory.id == memory_id)
                    .values(index_state="pending", updated_at=Memory.updated_at)
                )
                session.add(
                    OutboxEvent(
                        kind="index_memory",
                        payload={"memory_id": str(memory_id), "user_id": str(user_id)},
                    )
                )
            session.commit()
    return "purged"


def consolidate_conversation(
    rt: WorkerRuntime,
    conversation_id: uuid.UUID,
    user_id: uuid.UUID,
    extractor: MemoryExtractor | None = None,
) -> str:
    """Extract memories from the messages not yet examined. Consent-gated and idempotent.

    Messages written while memory was OFF are skipped for good (never examined retroactively
    after the user switches it on).
    """
    extractor = extractor or RuleExtractor()
    s = rt.settings
    with rt.session_factory() as session:
        conversation = session.execute(
            select(Conversation)
            .where(
                Conversation.id == conversation_id,
                Conversation.user_id == user_id,
                Conversation.deleted_at.is_(None),
            )
            .with_for_update()
        ).scalar_one_or_none()
        if conversation is None:
            return "gone"
        prefs = session.get(UserPreferences, user_id)
        if prefs is None or not prefs.memory_enabled:
            conversation.memory_seq = conversation.last_seq
            session.commit()
            return "consent_off"
        messages = list(
            session.execute(
                select(Message)
                .where(
                    Message.conversation_id == conversation_id,
                    Message.user_id == user_id,
                    Message.seq > conversation.memory_seq,
                )
                .order_by(Message.seq)
                .limit(CONSOLIDATION_BATCH)
            ).scalars()
        )
        if not messages:
            return "nothing_new"
        candidates = extractor.extract([ConversationTurn(m.role, m.content) for m in messages])
        live = session.execute(
            select(func.count())
            .select_from(Memory)
            .where(Memory.user_id == user_id, Memory.deleted_at.is_(None))
        ).scalar_one()
        created = 0
        for candidate in candidates:
            if live + created >= s.memory_max_per_user:
                break
            if _accept(candidate, s.memory_max_chars) is False:
                continue
            digest = content_hash(candidate.content)
            if session.execute(
                select(Memory.id).where(
                    Memory.user_id == user_id,
                    Memory.content_hash == digest,
                    Memory.deleted_at.is_(None),
                )
            ).first():
                continue
            memory = Memory(
                id=uuid.uuid4(),
                user_id=user_id,
                kind=candidate.kind,
                content=candidate.content,
                content_hash=digest,
                importance=candidate.importance,
                source="consolidation",
                source_conversation_id=conversation_id,
                index_state="pending",
            )
            session.add(memory)
            session.add(
                OutboxEvent(
                    kind="index_memory",
                    payload={"memory_id": str(memory.id), "user_id": str(user_id)},
                )
            )
            created += 1
        conversation.memory_seq = messages[-1].seq
        session.commit()
    log.info("memory_consolidated", conversation_id=str(conversation_id), created=created)
    return f"created:{created}"


def _accept(candidate: Candidate, max_chars: int) -> bool:
    return (
        candidate.kind in ("semantic", "episodic", "summary")
        and 1 <= len(candidate.content) <= max_chars
        and sensitive_reason(candidate.content) is None
    )


def sweep_retention(session: Session, *, batch: int = 500) -> int:
    """Tombstone memories not updated or used within the owner's retention window."""
    now = _now()
    rows = session.execute(
        text(
            "SELECT m.id, m.user_id FROM memories m "
            "LEFT JOIN user_preferences p ON p.user_id = m.user_id "
            "WHERE m.deleted_at IS NULL AND "
            "COALESCE(m.last_used_at, m.updated_at) < "
            ":now - make_interval(days => COALESCE(p.memory_retention_days, 365)) "
            "LIMIT :batch"
        ),
        {"now": now, "batch": batch},
    ).all()
    for memory_id, user_id in rows:
        session.execute(update(Memory).where(Memory.id == memory_id).values(deleted_at=now))
        session.add(
            OutboxEvent(
                kind=KIND_PURGE_MEMORY,
                payload={"memory_id": str(memory_id), "user_id": str(user_id)},
            )
        )
    session.commit()
    if rows:
        log.info("memory_retention_applied", memories=len(rows))
    return len(rows)


def retention_cutoff(days: int) -> datetime:
    return _now() - timedelta(days=days)
