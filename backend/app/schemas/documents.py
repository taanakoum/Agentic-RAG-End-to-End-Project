"""Document and ingestion API contracts (never expose ORM models, keys or hashes)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class DocumentError(BaseModel):
    code: str
    message: str
    recoverable: bool


class DocumentPublic(BaseModel):
    id: uuid.UUID
    filename: str
    size_bytes: int
    status: str
    page_count: int | None
    chunk_count: int | None
    ocr_status: str
    ocr_pages: int
    version: int
    latest_job_id: uuid.UUID | None
    error: DocumentError | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None


class UploadResponse(BaseModel):
    document: DocumentPublic
    job_id: uuid.UUID
    duplicate: bool = Field(description="True if an identical file was already uploaded")


class DocumentList(BaseModel):
    items: list[DocumentPublic]
    total: int
    limit: int
    offset: int


class JobPublic(BaseModel):
    id: uuid.UUID
    document_id: uuid.UUID
    version: int
    status: str
    attempts: int
    error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class JobAccepted(BaseModel):
    document_id: uuid.UUID
    job_id: uuid.UUID
    status: str


class DeleteAccepted(BaseModel):
    document_id: uuid.UUID
    status: str = "deleting"
