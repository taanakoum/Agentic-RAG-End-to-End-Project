"""Transactional outbox dispatcher: publishes committed outbox rows to Celery (at-least-once)."""

from __future__ import annotations

from celery import Celery
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.logging import get_logger
from app.ingestion.store import utcnow
from app.models.document import OutboxEvent

log = get_logger(__name__)

KIND_INGEST = "ingest_document"
KIND_PURGE = "purge_document"

TASK_BY_KIND = {
    KIND_INGEST: "ingestion.process_document",
    KIND_PURGE: "ingestion.purge_document",
}


def _args_for(event: OutboxEvent) -> list[str]:
    payload = event.payload
    if event.kind == KIND_INGEST:
        return [str(payload["job_id"])]
    return [str(payload["document_id"]), str(payload["user_id"])]


def dispatch_outbox(
    session_factory: sessionmaker[Session], celery: Celery, *, batch_size: int
) -> int:
    """Publish up to `batch_size` unsent events. Returns how many were sent.

    `FOR UPDATE SKIP LOCKED` lets several dispatchers (beat, API fast path, other nodes) run
    concurrently without sending the same row twice. A crash between publish and commit just
    re-publishes later; the tasks are idempotent.
    """
    sent = 0
    with session_factory() as session:
        events = (
            session.execute(
                select(OutboxEvent)
                .where(OutboxEvent.sent_at.is_(None))
                .order_by(OutboxEvent.id)
                .limit(batch_size)
                .with_for_update(skip_locked=True)
            )
            .scalars()
            .all()
        )
        for event in events:
            try:
                celery.send_task(TASK_BY_KIND[event.kind], args=_args_for(event))
            except Exception as exc:
                # Broker down or unknown kind: record and leave the row unsent for the next pass.
                event.attempts += 1
                event.last_error = f"{type(exc).__name__}: {exc}"[:500]
                log.warning("outbox_publish_failed", event_id=event.id, error=type(exc).__name__)
                continue
            event.sent_at = utcnow()
            event.attempts += 1
            sent += 1
        session.commit()
    return sent
