"""Reconciler: repairs work lost on the happy path (lost messages, crashes, stuck purges)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, exists, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.logging import get_logger
from app.ingestion.outbox import KIND_INGEST, KIND_PURGE, KIND_PURGE_USER
from app.ingestion.states import JobStatus
from app.ingestion.store import IngestionStore, utcnow
from app.models.document import Document, IngestionJob, OutboxEvent
from app.models.user import User

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ReconcileReport:
    requeued_queued: int = 0
    recovered_processing: int = 0
    failed_exhausted: int = 0
    purges_requeued: int = 0
    user_purges_requeued: int = 0


def _recent_event_exists(
    session: Session, kind: str, key: str, value: str, *, since: datetime
) -> bool:
    return bool(
        session.execute(
            select(
                exists().where(
                    OutboxEvent.kind == kind,
                    OutboxEvent.payload[key].astext == value,
                    OutboxEvent.created_at > since,
                )
            )
        ).scalar()
    )


def reconcile(session: Session, settings: Settings) -> ReconcileReport:
    now = utcnow()
    store = IngestionStore(session)
    queued = recovered = exhausted = purges = 0

    # 1. queued jobs whose message was lost: no outbox event created recently -> publish again
    queued_cutoff = now - timedelta(seconds=settings.reconcile_queued_ttl_s)
    stale_queued = (
        session.execute(
            select(IngestionJob.id).where(
                IngestionJob.status == JobStatus.QUEUED.value,
                IngestionJob.updated_at < queued_cutoff,
            )
        )
        .scalars()
        .all()
    )
    for job_id in stale_queued:
        if not _recent_event_exists(
            session, KIND_INGEST, "job_id", str(job_id), since=queued_cutoff
        ):
            store.add_outbox(KIND_INGEST, {"job_id": str(job_id)})
            queued += 1

    # 2. jobs stuck in processing (worker crashed): back to queued, or give up after N attempts
    processing_cutoff = now - timedelta(seconds=settings.reconcile_processing_ttl_s)
    stuck = session.execute(
        select(IngestionJob.id, IngestionJob.document_id, IngestionJob.attempts).where(
            IngestionJob.status == JobStatus.PROCESSING.value,
            IngestionJob.updated_at < processing_cutoff,
        )
    ).all()
    for job_id, document_id, attempts in stuck:
        if attempts >= settings.ingestion_max_attempts:
            store.mark_failed(
                document_id,
                job_id,
                code="INGESTION_FAILED",
                message="Processing kept failing or the worker stopped repeatedly.",
                recoverable=True,
            )
            exhausted += 1
            continue
        store.release_for_retry(job_id, document_id, "worker did not finish (recovered)")
        store.add_outbox(KIND_INGEST, {"job_id": str(job_id)})
        recovered += 1

    # 3. tombstoned documents whose purge has not finished: publish a purge again
    purge_cutoff = now - timedelta(seconds=settings.reconcile_purge_ttl_s)
    tombstones = session.execute(
        select(Document.id, Document.user_id).where(
            Document.deleted_at.is_not(None), Document.deleted_at < purge_cutoff
        )
    ).all()
    for document_id, user_id in tombstones:
        if not _recent_event_exists(
            session, KIND_PURGE, "document_id", str(document_id), since=purge_cutoff
        ):
            store.add_outbox(KIND_PURGE, {"document_id": str(document_id), "user_id": str(user_id)})
            purges += 1

    # 4. accounts whose deletion has not finished: publish the user purge again
    user_purges = 0
    for (user_id,) in session.execute(
        select(User.id).where(User.deleted_at.is_not(None), User.deleted_at < purge_cutoff)
    ).all():
        if not _recent_event_exists(
            session, KIND_PURGE_USER, "user_id", str(user_id), since=purge_cutoff
        ):
            store.add_outbox(KIND_PURGE_USER, {"user_id": str(user_id)})
            user_purges += 1

    pruned = prune_outbox(session)
    report = ReconcileReport(queued, recovered, exhausted, purges, user_purges)
    if any((queued, recovered, exhausted, purges, user_purges, pruned)):
        log.info(
            "reconciled",
            requeued_queued=queued,
            recovered_processing=recovered,
            failed_exhausted=exhausted,
            purges_requeued=purges,
            outbox_pruned=pruned,
        )
    return report


def prune_outbox(session: Session, *, older_than_days: int = 7) -> int:
    """Delete outbox rows that were published more than `older_than_days` ago."""
    cutoff = utcnow() - timedelta(days=older_than_days)
    result = session.execute(
        delete(OutboxEvent).where(OutboxEvent.sent_at.is_not(None), OutboxEvent.sent_at < cutoff)
    )
    session.commit()
    return int(getattr(result, "rowcount", 0) or 0)
