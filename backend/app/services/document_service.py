"""Document upload, listing, deletion and retry (API side; heavy work happens in Celery)."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import BinaryIO

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.core.exceptions import (
    ConcurrencyConflictError,
    DocumentValidationError,
    ExternalServiceUnavailableError,
    ResourceNotFoundError,
)
from app.core.logging import get_logger
from app.infra.rate_limit import RateLimiter, subject_key
from app.ingestion.outbox import dispatch_outbox
from app.ingestion.pipeline import object_key
from app.ingestion.states import DocumentStatus
from app.ingestion.validation import ValidatedPdf, safe_filename, validate_pdf
from app.models.document import Document, IngestionJob
from app.repositories.documents import DocumentRepository
from app.schemas.documents import (
    DocumentError,
    DocumentList,
    DocumentPublic,
    JobAccepted,
    JobPublic,
)
from app.services.auth_service import AuthenticatedUser
from app.storage.base import ObjectStorage, ObjectTooLargeError, StorageError
from app.tasks.celery_app import celery_app

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class UploadResult:
    document: DocumentPublic
    job_id: uuid.UUID
    duplicate: bool


def to_public(doc: Document, latest_job_id: uuid.UUID | None) -> DocumentPublic:
    error = (
        DocumentError(
            code=doc.error_code or "INGESTION_FAILED",
            message=doc.error_message or "Processing failed.",
            recoverable=bool(doc.error_recoverable),
        )
        if doc.status == DocumentStatus.FAILED.value
        else None
    )
    return DocumentPublic(
        id=doc.id,
        filename=doc.filename,
        size_bytes=doc.size_bytes,
        status=doc.status,
        page_count=doc.page_count,
        chunk_count=doc.chunk_count,
        ocr_status=doc.ocr_status,
        ocr_pages=doc.ocr_pages,
        version=doc.version,
        latest_job_id=latest_job_id,
        error=error,
        created_at=doc.created_at,
        updated_at=doc.updated_at,
        completed_at=doc.completed_at,
    )


def to_job_public(job: IngestionJob) -> JobPublic:
    return JobPublic(
        id=job.id,
        document_id=job.document_id,
        version=job.version,
        status=job.status,
        attempts=job.attempts,
        error=job.last_error,
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
    )


class DocumentService:
    def __init__(
        self,
        *,
        session: AsyncSession,
        settings: Settings,
        storage: ObjectStorage,
        limiter: RateLimiter,
        sync_session_factory: sessionmaker[Session],
    ) -> None:
        self._session = session
        self._settings = settings
        self._storage = storage
        self._limiter = limiter
        self._sync_sf = sync_session_factory
        self._docs = DocumentRepository(session)

    # ------------------------------------------------------------------ upload

    async def upload(
        self,
        user: AuthenticatedUser,
        file: BinaryIO,
        *,
        filename: str | None,
        content_type: str | None,
    ) -> UploadResult:
        s = self._settings
        await self._limiter.hit(
            "upload",
            subject_key(str(user.id)),
            limit=s.rl_upload_limit,
            window_s=s.rl_upload_window_s,
        )
        validated = await self._validate(file, filename, content_type)

        document_id = uuid.uuid4()
        key = object_key(user.id, document_id)
        try:
            stored = await asyncio.to_thread(
                self._storage.put, key, file, max_bytes=s.max_pdf_bytes
            )
        except ObjectTooLargeError as exc:
            raise DocumentValidationError(
                "The PDF is too large.", code="DOCUMENT_TOO_LARGE"
            ) from exc
        except StorageError as exc:
            log.error("upload_storage_failed", error=type(exc).__name__)
            raise ExternalServiceUnavailableError("File storage is unavailable.") from exc
        if stored.sha256 != validated.sha256:  # the bytes changed between validation and storing
            await asyncio.to_thread(self._storage.delete, key)
            raise DocumentValidationError(
                "The upload was corrupted in transit. Please try again.", code="DOCUMENT_CORRUPTED"
            )

        try:
            document, job = await self._docs.insert_or_get(
                document_id=document_id,
                user_id=user.id,
                filename=safe_filename(filename),
                content_hash=validated.sha256,
                size_bytes=validated.size,
                mime_type="application/pdf",
                storage_key=key,
                page_count=validated.page_count,
            )
            await self._session.commit()
        except BaseException:
            # Row never committed: do not leave an orphaned object behind.
            await self._session.rollback()
            await asyncio.to_thread(self._storage.delete, key)
            raise

        if job is None:
            # Lost the duplicate race (or it was a plain re-upload): drop our redundant copy.
            await asyncio.to_thread(self._storage.delete, key)
            latest = (await self._docs.latest_job_ids([document.id])).get(document.id)
            if latest is None:  # an existing live document always has a job; defensive
                raise ResourceNotFoundError()
            return UploadResult(to_public(document, latest), latest, duplicate=True)

        await self._kick_dispatcher()
        return UploadResult(to_public(document, job.id), job.id, duplicate=False)

    async def _validate(
        self, file: BinaryIO, filename: str | None, content_type: str | None
    ) -> ValidatedPdf:
        s = self._settings
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(
                    validate_pdf, file, filename=filename, content_type=content_type, settings=s
                ),
                timeout=s.upload_validation_timeout_s,
            )
        except TimeoutError as exc:
            raise DocumentValidationError(
                "The PDF took too long to inspect and was rejected.",
                code="DOCUMENT_PARSE_TIMEOUT",
            ) from exc

    async def _kick_dispatcher(self) -> None:
        """Best-effort low-latency publish. Beat + the reconciler guarantee delivery regardless."""
        try:
            await asyncio.to_thread(
                dispatch_outbox,
                self._sync_sf,
                celery_app,
                batch_size=self._settings.outbox_batch_size,
            )
        except Exception:
            log.warning("dispatcher_kick_failed")

    # ------------------------------------------------------------------ reads

    async def list(
        self, user: AuthenticatedUser, *, limit: int, offset: int, status: str | None
    ) -> DocumentList:
        docs, total = await self._docs.list_owned(
            user.id, limit=limit, offset=offset, status=status
        )
        latest = await self._docs.latest_job_ids([d.id for d in docs])
        return DocumentList(
            items=[to_public(d, latest.get(d.id)) for d in docs],
            total=total,
            limit=limit,
            offset=offset,
        )

    async def get(self, user: AuthenticatedUser, document_id: uuid.UUID) -> DocumentPublic:
        doc = await self._docs.get_owned(user.id, document_id)
        if doc is None:
            raise ResourceNotFoundError()
        return to_public(doc, (await self._docs.latest_job_ids([doc.id])).get(doc.id))

    async def get_job(self, user: AuthenticatedUser, job_id: uuid.UUID) -> JobPublic:
        job = await self._docs.get_job_owned(user.id, job_id)
        if job is None:
            raise ResourceNotFoundError()
        return to_job_public(job)

    # ------------------------------------------------------------------ writes

    async def delete(self, user: AuthenticatedUser, document_id: uuid.UUID) -> None:
        """Tombstone now (the document disappears from the API); purge happens asynchronously."""
        if not await self._docs.tombstone(user.id, document_id, now=datetime.now(UTC)):
            await self._session.rollback()
            raise ResourceNotFoundError()
        await self._session.commit()
        await self._kick_dispatcher()

    async def retry(self, user: AuthenticatedUser, job_id: uuid.UUID) -> JobAccepted:
        job = await self._docs.get_job_owned(user.id, job_id)
        if job is None:
            raise ResourceNotFoundError()
        doc = await self._docs.get_owned(user.id, job.document_id)
        if doc is None:
            raise ResourceNotFoundError()
        if doc.status != DocumentStatus.FAILED.value or doc.error_recoverable is not True:
            raise ConcurrencyConflictError(
                "Only documents whose processing failed with a recoverable error can be retried.",
                code="RETRY_NOT_ALLOWED",
            )
        new_job = await self._docs.requeue(doc, expected=DocumentStatus.FAILED, bump_version=False)
        if new_job is None:  # someone else retried first
            await self._session.rollback()
            raise ConcurrencyConflictError(
                "The document was already retried.", code="RETRY_NOT_ALLOWED"
            )
        await self._session.commit()
        await self._kick_dispatcher()
        return JobAccepted(
            document_id=doc.id, job_id=new_job.id, status=DocumentStatus.QUEUED.value
        )

    async def reindex(self, user: AuthenticatedUser, document_id: uuid.UUID) -> JobAccepted:
        """Explicit re-index into a new document version (old vectors are removed afterwards)."""
        doc = await self._docs.get_owned(user.id, document_id)
        if doc is None:
            raise ResourceNotFoundError()
        expected = DocumentStatus(doc.status)
        if expected not in {DocumentStatus.COMPLETED, DocumentStatus.FAILED}:
            raise ConcurrencyConflictError(
                "The document is still being processed.", code="REINDEX_NOT_ALLOWED"
            )
        new_job = await self._docs.requeue(doc, expected=expected, bump_version=True)
        if new_job is None:
            await self._session.rollback()
            raise ConcurrencyConflictError(
                "The document changed state; try again.", code="REINDEX_NOT_ALLOWED"
            )
        await self._session.commit()
        await self._kick_dispatcher()
        return JobAccepted(
            document_id=doc.id, job_id=new_job.id, status=DocumentStatus.QUEUED.value
        )
