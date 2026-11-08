"""Async document persistence for the API. Every query is scoped by the owning user."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

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
        source_format: str = "pdf",
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
                source_format=source_format,
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

    async def readable(
        self, user_id: uuid.UUID, only: Sequence[uuid.UUID] | None = None
    ) -> dict[uuid.UUID, tuple[str, int]]:
        """Documents this user may retrieve from: owned, COMPLETED and not deleted.

        Returns id -> (filename, current version). `only` narrows to the given ids; ids that are
        not readable simply do not appear, so a caller cannot tell "not yours" from "unknown".
        """
        stmt = select(Document.id, Document.filename, Document.version).where(
            Document.user_id == user_id,
            Document.status == DocumentStatus.COMPLETED.value,
            Document.deleted_at.is_(None),
        )
        if only is not None:
            stmt = stmt.where(Document.id.in_(list(only)))
        rows = await self._session.execute(stmt)
        return {row.id: (row.filename, row.version) for row in rows}

    def _latest_job_id(self) -> Any:
        return (
            select(IngestionJob.id)
            .where(IngestionJob.document_id == Document.id)
            .order_by(IngestionJob.created_at.desc())
            .limit(1)
            .scalar_subquery()
        )

    async def get_owned_with_job(
        self, user_id: uuid.UUID, document_id: uuid.UUID
    ) -> tuple[Document, uuid.UUID | None] | None:
        """The document and its latest job id in one round trip."""
        row = (
            await self._session.execute(
                select(Document, self._latest_job_id()).where(
                    Document.id == document_id,
                    Document.user_id == user_id,
                    Document.deleted_at.is_(None),
                )
            )
        ).first()
        return None if row is None else (row[0], row[1])

    async def list_owned(
        self, user_id: uuid.UUID, *, limit: int, offset: int, status: str | None
    ) -> tuple[list[tuple[Document, uuid.UUID | None]], int]:
        """A page of documents with latest job ids and the total, in ONE query (window count)."""
        query = select(Document, self._latest_job_id(), func.count().over()).where(
            Document.user_id == user_id, Document.deleted_at.is_(None)
        )
        if status is not None:
            query = query.where(Document.status == status)
        rows = (
            await self._session.execute(
                query.order_by(Document.created_at.desc(), Document.id).limit(limit).offset(offset)
            )
        ).all()
        if rows:
            return [(r[0], r[1]) for r in rows], int(rows[0][2])
        if offset == 0:
            return [], 0
        # a page past the end: the window count has no row to ride on, so count separately
        count = (
            select(func.count())
            .select_from(Document)
            .where(Document.user_id == user_id, Document.deleted_at.is_(None))
        )
        if status is not None:
            count = count.where(Document.status == status)
        return [], int((await self._session.execute(count)).scalar_one())

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

    async def tombstone_all(self, user_id: uuid.UUID, *, now: datetime) -> int:
        """Soft-delete every live document of a user and queue a purge event for each."""
        result = await self._session.execute(
            update(Document)
            .where(Document.user_id == user_id, Document.deleted_at.is_(None))
            .values(deleted_at=now)
            .returning(Document.id)
        )
        ids = [row[0] for row in result.all()]
        for document_id in ids:
            self._session.add(
                OutboxEvent(
                    kind=KIND_PURGE,
                    payload={"document_id": str(document_id), "user_id": str(user_id)},
                )
            )
        await self._session.flush()
        return len(ids)
