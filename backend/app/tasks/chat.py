"""Celery tasks for conversations: purge after deletion, orphan checkpoint sweep."""

from __future__ import annotations

import uuid
from typing import Any

from celery import Task

from app.chat.purge import purge_conversation, sweep_orphan_checkpoints
from app.core.logging import get_logger
from app.ingestion.runtime import get_runtime
from app.tasks.celery_app import celery_app
from app.tasks.ingestion import backoff_seconds

log = get_logger(__name__)


@celery_app.task(
    name="chat.purge_conversation",
    bind=True,
    acks_late=True,
    reject_on_worker_lost=True,
)
def purge_conversation_task(self: Task[Any, Any], conversation_id: str, user_id: str) -> str:
    rt = get_runtime()
    retries = int(self.request.retries or 0)
    try:
        with rt.session_factory() as session:
            return purge_conversation(session, uuid.UUID(conversation_id), uuid.UUID(user_id))
    except Exception as exc:
        log.warning("conversation_purge_failed", error=type(exc).__name__)
        raise self.retry(
            exc=exc,
            countdown=backoff_seconds(retries, base=rt.settings.ingestion_retry_base_s),
            max_retries=rt.settings.ingestion_max_attempts + 5,
        ) from exc


@celery_app.task(name="chat.sweep_checkpoints")
def sweep_checkpoints_task() -> int:
    rt = get_runtime()
    with rt.session_factory() as session:
        return sweep_orphan_checkpoints(session)
