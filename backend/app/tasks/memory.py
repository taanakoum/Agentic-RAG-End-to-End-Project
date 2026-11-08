"""Celery tasks for long-term memory."""

from __future__ import annotations

import uuid
from typing import Any

from celery import Task

from app.core.logging import get_logger
from app.ingestion.outbox import dispatch_outbox
from app.ingestion.runtime import get_runtime
from app.memory.pipeline import (
    consolidate_conversation,
    index_memory,
    purge_memory,
    purge_user_memories,
    sweep_retention,
)
from app.tasks.celery_app import celery_app
from app.tasks.ingestion import backoff_seconds

log = get_logger(__name__)


def _publish_now() -> None:
    """Publish the events this task just wrote (Beat is the fallback), for low latency."""
    rt = get_runtime()
    try:
        dispatch_outbox(rt.session_factory, celery_app, batch_size=rt.settings.outbox_batch_size)
    except Exception as exc:  # best effort: Beat publishes anyway
        log.warning("memory_publish_failed", error=type(exc).__name__)


def _retry(self: Task[Any, Any], exc: Exception, what: str) -> Exception:
    rt = get_runtime()
    log.warning(f"{what}_failed", error=type(exc).__name__)
    return self.retry(
        exc=exc,
        countdown=backoff_seconds(
            int(self.request.retries or 0), base=rt.settings.ingestion_retry_base_s
        ),
        max_retries=rt.settings.ingestion_max_attempts + 5,
    )


@celery_app.task(name="memory.index_memory", bind=True, acks_late=True, reject_on_worker_lost=True)
def index_memory_task(self: Task[Any, Any], memory_id: str, user_id: str) -> str:
    try:
        return index_memory(get_runtime(), uuid.UUID(memory_id), uuid.UUID(user_id))
    except Exception as exc:
        raise _retry(self, exc, "memory_index") from exc


@celery_app.task(name="memory.purge_memory", bind=True, acks_late=True, reject_on_worker_lost=True)
def purge_memory_task(self: Task[Any, Any], memory_id: str, user_id: str) -> str:
    try:
        return purge_memory(get_runtime(), uuid.UUID(memory_id), uuid.UUID(user_id))
    except Exception as exc:
        raise _retry(self, exc, "memory_purge") from exc


@celery_app.task(
    name="memory.purge_user_memories", bind=True, acks_late=True, reject_on_worker_lost=True
)
def purge_user_memories_task(self: Task[Any, Any], user_id: str) -> str:
    try:
        return purge_user_memories(get_runtime(), uuid.UUID(user_id))
    except Exception as exc:
        raise _retry(self, exc, "memory_reset") from exc


@celery_app.task(
    name="memory.consolidate_conversation", bind=True, acks_late=True, reject_on_worker_lost=True
)
def consolidate_conversation_task(self: Task[Any, Any], conversation_id: str, user_id: str) -> str:
    try:
        result = consolidate_conversation(
            get_runtime(), uuid.UUID(conversation_id), uuid.UUID(user_id)
        )
        _publish_now()
        return result
    except Exception as exc:
        raise _retry(self, exc, "memory_consolidation") from exc


@celery_app.task(name="memory.sweep_retention")
def sweep_retention_task() -> int:
    rt = get_runtime()
    with rt.session_factory() as session:
        removed = sweep_retention(session)
    _publish_now()
    return removed
