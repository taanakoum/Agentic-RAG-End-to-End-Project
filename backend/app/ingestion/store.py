"""Synchronous document/job persistence for workers: compare-and-set state transitions."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.ingestion.states import DocumentStatus, JobStatus, check_transition
from app.models.document import Document, IngestionJob, OutboxEvent


class LostStateError(Exception):
    """The document is no longer in the state this worker expected (moved, deleted, or purged)."""


@dataclass(frozen=True, slots=True)
class JobClaim:
    job_id: uuid.UUID
    document_id: uuid.UUID
    user_id: uuid.UUID
    version: int


def utcnow() -> datetime:
    return datetime.now(UTC)


class IngestionStore:
    def __init__(self, session: Session) -> None:
        self._s = session

    # ------------------------------------------------------------------ jobs

    def claim_job(self, job_id: uuid.UUID) -> JobClaim | str:
        """Atomically move queued -> processing. Returns the claim, or why it was skipped.

        Duplicate deliveries of the same task see processing/succeeded here and skip.
        """
        now = utcnow()
        row = self._s.execute(
            update(IngestionJob)
            .where(IngestionJob.id == job_id, IngestionJob.status == JobStatus.QUEUED.value)
            .values(
                status=JobStatus.PROCESSING.value,
                attempts=IngestionJob.attempts + 1,
                started_at=now,
                updated_at=now,
            )
            .returning(IngestionJob.document_id, IngestionJob.user_id, IngestionJob.version)
        ).first()
        self._s.commit()
        if row is not None:
            return JobClaim(job_id, row[0], row[1], row[2])
        job = self._s.get(IngestionJob, job_id)
        if job is None:
            return "job_not_found"
        self._s.refresh(job)
        return f"job_{job.status}"

    def get_job(self, job_id: uuid.UUID) -> IngestionJob | None:
        return self._s.get(IngestionJob, job_id)

    def finish_job(self, job_id: uuid.UUID, status: JobStatus, error: str | None = None) -> None:
        now = utcnow()
        self._s.execute(
            update(IngestionJob)
            .where(IngestionJob.id == job_id)
            .values(status=status.value, last_error=error, finished_at=now, updated_at=now)
        )
        self._s.commit()

    def release_for_retry(self, job_id: uuid.UUID, document_id: uuid.UUID, error: str) -> None:
        """Transient failure: job and document go back to queued so a retry can claim them."""
        now = utcnow()
        self._s.execute(
            update(IngestionJob)
            .where(IngestionJob.id == job_id, IngestionJob.status == JobStatus.PROCESSING.value)
            .values(status=JobStatus.QUEUED.value, last_error=error[:2000], updated_at=now)
        )
        self._s.execute(
            update(Document)
            .where(
                Document.id == document_id,
                Document.status.in_(
                    [
                        DocumentStatus.PROCESSING.value,
                        DocumentStatus.EXTRACTING.value,
                        DocumentStatus.CHUNKING.value,
                        DocumentStatus.EMBEDDING.value,
                        DocumentStatus.INDEXING.value,
                    ]
                ),
            )
            .values(status=DocumentStatus.QUEUED.value, updated_at=now)
        )
        self._s.commit()

    # ------------------------------------------------------------------ documents

    def get_document(self, document_id: uuid.UUID) -> Document | None:
        doc = self._s.get(Document, document_id)
        if doc is not None:
            self._s.refresh(doc)
        return doc

    def transition(
        self,
        document_id: uuid.UUID,
        expected: DocumentStatus,
        new: DocumentStatus,
        **fields: Any,
    ) -> None:
        """Compare-and-set status change. Raises IllegalTransitionError or LostStateError."""
        check_transition(expected, new)
        now = utcnow()
        values: dict[str, Any] = {"status": new.value, "updated_at": now, **fields}
        updated = self._s.execute(
            update(Document)
            .where(
                Document.id == document_id,
                Document.status == expected.value,
                Document.deleted_at.is_(None),
            )
            .values(**values)
            .returning(Document.id)
        ).first()
        self._s.commit()
        if updated is None:
            raise LostStateError(f"document {document_id} is no longer {expected.value}")
        # keep the job's heartbeat fresh so the reconciler does not treat it as stuck
        self._s.execute(
            update(IngestionJob)
            .where(
                IngestionJob.document_id == document_id,
                IngestionJob.status == JobStatus.PROCESSING.value,
            )
            .values(updated_at=now)
        )
        self._s.commit()

    def mark_failed(
        self,
        document_id: uuid.UUID,
        job_id: uuid.UUID,
        *,
        code: str,
        message: str,
        recoverable: bool,
    ) -> None:
        """Move the document to FAILED from whatever active state it is in; fail the job."""
        now = utcnow()
        self._s.execute(
            update(Document)
            .where(
                Document.id == document_id,
                Document.status != DocumentStatus.COMPLETED.value,
                Document.deleted_at.is_(None),
            )
            .values(
                status=DocumentStatus.FAILED.value,
                error_code=code,
                error_message=message[:1000],
                error_recoverable=recoverable,
                updated_at=now,
            )
        )
        self._s.execute(
            update(IngestionJob)
            .where(IngestionJob.id == job_id)
            .values(
                status=JobStatus.FAILED.value,
                last_error=f"{code}: {message}"[:2000],
                finished_at=now,
                updated_at=now,
            )
        )
        self._s.commit()

    def complete(
        self, document_id: uuid.UUID, job_id: uuid.UUID, *, chunk_count: int, embedding_model: str
    ) -> None:
        self.transition(
            document_id,
            DocumentStatus.INDEXING,
            DocumentStatus.COMPLETED,
            chunk_count=chunk_count,
            embedding_model=embedding_model,
            completed_at=utcnow(),
            error_code=None,
            error_message=None,
            error_recoverable=None,
        )
        self.finish_job(job_id, JobStatus.SUCCEEDED)

    def has_processing_job(self, document_id: uuid.UUID) -> bool:
        row = self._s.execute(
            select(IngestionJob.id)
            .where(
                IngestionJob.document_id == document_id,
                IngestionJob.status == JobStatus.PROCESSING.value,
            )
            .limit(1)
        ).first()
        return row is not None

    def hard_delete_document(self, document_id: uuid.UUID) -> None:
        doc = self._s.get(Document, document_id)
        if doc is not None:
            self._s.delete(doc)  # jobs cascade
        self._s.commit()

    def add_outbox(self, kind: str, payload: dict[str, Any]) -> None:
        self._s.add(OutboxEvent(kind=kind, payload=payload))
        self._s.commit()
