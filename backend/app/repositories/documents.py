"""Async document persistence for the API. Every query is scoped by the owning user."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.ingestion.outbox import KIND_INGEST, KIND_PURGE
from app.ingestion.states import DocumentStatus, JobStatus
from app.models.document import Document, IngestionJob, OutboxEvent


class DocumentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ------------------------------------------------------------------ create (dedup)

    async def insert_or_get(
        self,
        *,
        document_id: uuid.UUID,
        user_id: uuid.UUID,
        filename: str,
        content_hash: str,
        size_bytes: int,
        mime_type: str,
        storage_key: str,
        page_count: int,
    ) -> tuple[Document, IngestionJob | None]:
        """Insert the document + job + outbox event in ONE transaction, or fetch the existing one.

        The partial unique index (user_id, content_hash WHERE deleted_at IS NULL) decides the
        winner of concurrent identical uploads; the loser gets (existing_document, None).
        Nothing is committed here: the caller owns the transaction.
        """
        inserted = await self._session.execute(
            pg_insert(Document)
            .values(
                id=document_id,
                user_id=user_id,
                filename=filename,
                content_hash=content_hash,
                size_bytes=size_bytes,
                mime_type=mime_type,
                storage_key=storage_key,
                status=DocumentStatus.QUEUED.value,
                page_count=page_count,
            )
            .on_conflict_do_nothing(
                index_elements=[Document.user_id, Document.content_hash],
                index_where=text("deleted_at IS NULL"),
            )
            .returning(Document.id)
        )
        if inserted.scalar_one_or_none() is None:
            existing = await self._session.execute(
                select(Document).where(
                    Document.user_id == user_id,
                    Document.content_hash == content_hash,
                    Document.deleted_at.is_(None),
                )
            )
            return existing.scalar_one(), None
        document = await self._session.get_one(Document, document_id)
        job = await self.add_job(document)
        return document, job

    async def add_job(self, document: Document) -> IngestionJob:
        job = IngestionJob(
            document_id=document.id,
            user_id=document.user_id,
            version=document.version,
            status=JobStatus.QUEUED.value,
        )
        self._session.add(job)
        await self._session.flush()
        self._session.add(OutboxEvent(kind=KIND_INGEST, payload={"job_id": str(job.id)}))
        await self._session.flush()
        return job

    # ------------------------------------------------------------------ owned reads

    async def get_owned(self, user_id: uuid.UUID, document_id: uuid.UUID) -> Document | None:
        result = await self._session.execute(
            select(Document).where(
                Document.id == document_id,
                Document.user_id == user_id,
                Document.deleted_at.is_(None),
            )
        )
        return result.scalar_one_or_none()

    async def list_owned(
        self, user_id: uuid.UUID, *, limit: int, offset: int, status: str | None
    ) -> tuple[list[Document], int]:
        base = select(Document).where(Document.user_id == user_id, Document.deleted_at.is_(None))
        count = (
            select(func.count())
            .select_from(Document)
            .where(Document.user_id == user_id, Document.deleted_at.is_(None))
        )
        if status is not None:
            base = base.where(Document.status == status)
            count = count.where(Document.status == status)
        rows = await self._session.execute(
            base.order_by(Document.created_at.desc(), Document.id).limit(limit).offset(offset)
        )
        total = (await self._session.execute(count)).scalar_one()
        return list(rows.scalars().all()), int(total)

    async def latest_job_ids(self, document_ids: list[uuid.UUID]) -> dict[uuid.UUID, uuid.UUID]:
        if not document_ids:
            return {}
        rows = await self._session.execute(
            select(IngestionJob.document_id, IngestionJob.id)
            .where(IngestionJob.document_id.in_(document_ids))
            .order_by(IngestionJob.document_id, IngestionJob.created_at.desc())
            .ext(distinct_on(IngestionJob.document_id))
        )
        return {doc_id: job_id for doc_id, job_id in rows.all()}

    async def get_job_owned(self, user_id: uuid.UUID, job_id: uuid.UUID) -> IngestionJob | None:
        result = await self._session.execute(
            select(IngestionJob)
            .join(Document, Document.id == IngestionJob.document_id)
            .where(
                IngestionJob.id == job_id,
                IngestionJob.user_id == user_id,
                Document.user_id == user_id,
                Document.deleted_at.is_(None),
            )
        )
        return result.scalar_one_or_none()

    # ------------------------------------------------------------------ owned writes

    async def requeue(
        self,
        document: Document,
        *,
        expected: DocumentStatus,
        bump_version: bool,
    ) -> IngestionJob | None:
        """failed/completed -> queued with a new job (compare-and-set on status)."""
        values: dict[str, object] = {
            "status": DocumentStatus.QUEUED.value,
            "error_code": None,
            "error_message": None,
            "error_recoverable": None,
        }
        if bump_version:
            values["version"] = Document.version + 1
        result = await self._session.execute(
            update(Document)
            .where(
                Document.id == document.id,
                Document.user_id == document.user_id,
                Document.status == expected.value,
                Document.deleted_at.is_(None),
            )
            .values(**values)
            .returning(Document.id)
        )
        if result.scalar_one_or_none() is None:
            return None
        await self._session.refresh(document)
        return await self.add_job(document)

    async def tombstone(self, user_id: uuid.UUID, document_id: uuid.UUID, *, now: datetime) -> bool:
        """Soft-delete + purge event in one transaction. False if not found / already deleted."""
        result = await self._session.execute(
            update(Document)
            .where(
                Document.id == document_id,
                Document.user_id == user_id,
                Document.deleted_at.is_(None),
            )
            .values(deleted_at=now)
            .returning(Document.id)
        )
        if result.scalar_one_or_none() is None:
            return False
        self._session.add(
            OutboxEvent(
                kind=KIND_PURGE,
                payload={"document_id": str(document_id), "user_id": str(user_id)},
            )
        )
        await self._session.flush()
        return True
