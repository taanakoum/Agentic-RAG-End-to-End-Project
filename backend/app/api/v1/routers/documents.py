"""Document endpoints. HTTP only; logic lives in DocumentService."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, File, Query, Response, UploadFile, status

from app.core.exceptions import error_responses
from app.dependencies.auth import CurrentUser, DocumentServiceDep
from app.schemas.documents import (
    DeleteAccepted,
    DocumentList,
    DocumentPublic,
    JobAccepted,
    UploadResponse,
)

router = APIRouter(prefix="/documents", tags=["documents"])


@router.post(
    "/upload",
    response_model=UploadResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses=error_responses(400, 401, 413, 415, 422, 429, 503),
)
async def upload_document(
    response: Response,
    current: CurrentUser,
    service: DocumentServiceDep,
    file: Annotated[UploadFile, File(description="The PDF to ingest")],
) -> UploadResponse:
    """Validate and store a PDF, then queue background ingestion.

    Returns 202 for a new document and 200 when an identical file was uploaded before (the
    existing document is returned; nothing is re-processed).
    """
    result = await service.upload(
        current, file.file, filename=file.filename, content_type=file.content_type
    )
    if result.duplicate:
        response.status_code = status.HTTP_200_OK
    return UploadResponse(
        document=result.document, job_id=result.job_id, duplicate=result.duplicate
    )


@router.get("", response_model=DocumentList, responses=error_responses(401))
async def list_documents(
    current: CurrentUser,
    service: DocumentServiceDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
    doc_status: Annotated[str | None, Query(alias="status", max_length=16)] = None,
) -> DocumentList:
    return await service.list(current, limit=limit, offset=offset, status=doc_status)


@router.get("/{document_id}", response_model=DocumentPublic, responses=error_responses(401, 404))
async def get_document(
    document_id: uuid.UUID, current: CurrentUser, service: DocumentServiceDep
) -> DocumentPublic:
    return await service.get(current, document_id)


@router.get(
    "/{document_id}/status", response_model=DocumentPublic, responses=error_responses(401, 404)
)
async def get_document_status(
    document_id: uuid.UUID, current: CurrentUser, service: DocumentServiceDep
) -> DocumentPublic:
    """Alias of GET /documents/{id}; the ingestion router's job endpoints are canonical."""
    return await service.get(current, document_id)


@router.post(
    "/{document_id}/reindex",
    response_model=JobAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    responses=error_responses(401, 404, 409),
)
async def reindex_document(
    document_id: uuid.UUID, current: CurrentUser, service: DocumentServiceDep
) -> JobAccepted:
    return await service.reindex(current, document_id)


@router.delete(
    "/{document_id}",
    response_model=DeleteAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    responses=error_responses(401, 404),
)
async def delete_document(
    document_id: uuid.UUID, current: CurrentUser, service: DocumentServiceDep
) -> DeleteAccepted:
    """Tombstone the document immediately; vectors, files and rows are purged asynchronously."""
    await service.delete(current, document_id)
    return DeleteAccepted(document_id=document_id)
