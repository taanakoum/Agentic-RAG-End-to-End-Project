"""Celery tasks for ingestion. Plain sync functions using sync clients (ADR-003)."""

from __future__ import annotations

import random
import uuid
from typing import Any

from celery import Task
from celery.exceptions import SoftTimeLimitExceeded

from app.core.config import get_settings
from app.core.logging import get_logger
from app.ingestion.outbox import dispatch_outbox
from app.ingestion.pipeline import run_ingestion
from app.ingestion.purge import PurgeBusyError, purge_document
from app.ingestion.reconcile import reconcile
from app.ingestion.runtime import get_runtime
from app.ingestion.store import IngestionStore
from app.tasks.celery_app import celery_app

log = get_logger(__name__)


def backoff_seconds(retries: int, *, base: float) -> float:
    """Exponential backoff with jitter: base * 2^retries (capped at 5 min) + up to 25% jitter."""
    delay = min(300.0, base * (2**retries))
    return delay + random.uniform(0, delay * 0.25)  # noqa: S311  (jitter, not security)


@celery_app.task(
    name="ingestion.process_document",
    bind=True,
    acks_late=True,
    reject_on_worker_lost=True,
)
def process_document(self: Task[Any, Any], job_id: str) -> str:
    """Run the ingestion pipeline for one job; retry transient failures with backoff."""
    rt = get_runtime()
    settings = rt.settings
    job_uuid = uuid.UUID(job_id)
    try:
        return run_ingestion(rt, job_uuid)
    except (SoftTimeLimitExceeded, Exception) as exc:
        reason = f"{type(exc).__name__}: {exc}"[:500]
        with rt.session_factory() as session:
            store = IngestionStore(session)
            job = store.get_job(job_uuid)
            if job is None:
                raise
            document_id = job.document_id
            retries = int(self.request.retries or 0)
            if retries + 1 >= settings.ingestion_max_attempts:
                store.mark_failed(
                    document_id,
                    job_uuid,
                    code="INGESTION_FAILED",
                    message="Processing failed repeatedly. You can retry it.",
                    recoverable=True,
                )
                log.error("ingestion_retries_exhausted", job_id=job_id, error=type(exc).__name__)
                return "failed"
            store.release_for_retry(job_uuid, document_id, reason)
        log.warning("ingestion_transient_failure", job_id=job_id, retries=retries, reason=reason)
        raise self.retry(
            exc=exc,
            countdown=backoff_seconds(retries, base=settings.ingestion_retry_base_s),
            max_retries=settings.ingestion_max_attempts,
        ) from exc


@celery_app.task(
    name="ingestion.purge_document",
    bind=True,
    acks_late=True,
    reject_on_worker_lost=True,
)
def purge_document_task(self: Task[Any, Any], document_id: str, user_id: str) -> str:
    rt = get_runtime()
    retries = int(self.request.retries or 0)
    try:
        return purge_document(rt, uuid.UUID(document_id), uuid.UUID(user_id))
    except PurgeBusyError as exc:
        raise self.retry(exc=exc, countdown=5, max_retries=60) from exc
    except Exception as exc:
        log.warning("purge_failed", document_id=document_id, error=type(exc).__name__)
        raise self.retry(
            exc=exc,
            countdown=backoff_seconds(retries, base=rt.settings.ingestion_retry_base_s),
            max_retries=rt.settings.ingestion_max_attempts + 5,
        ) from exc


@celery_app.task(name="ingestion.dispatch_outbox")
def dispatch_outbox_task() -> int:
    rt = get_runtime()
    return dispatch_outbox(rt.session_factory, celery_app, batch_size=rt.settings.outbox_batch_size)


@celery_app.task(name="ingestion.reconcile")
def reconcile_task() -> dict[str, int]:
    rt = get_runtime()
    with rt.session_factory() as session:
        report = reconcile(session, get_settings())
    return {
        "requeued_queued": report.requeued_queued,
        "recovered_processing": report.recovered_processing,
        "failed_exhausted": report.failed_exhausted,
        "purges_requeued": report.purges_requeued,
    }
