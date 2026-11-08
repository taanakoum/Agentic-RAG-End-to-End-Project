"""Qdrant collection layout and idempotent chunk indexing (sync client; used by Celery tasks).

Layout (ADR-012): one collection `<prefix>chunks`, named vectors `dense` (cosine) and `sparse`
(IDF-modified), keyword payload indexes on user_id (tenant) and document_id. Point id =
uuid5(document_id:version:chunk_index), so re-running a job overwrites instead of duplicating.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from qdrant_client import QdrantClient
from qdrant_client.http.exceptions import UnexpectedResponse
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    FilterSelector,
    KeywordIndexParams,
    KeywordIndexType,
    MatchValue,
    Modifier,
    PayloadSchemaType,
    PointStruct,
    SparseVector,
    SparseVectorParams,
    VectorParams,
)

from app.core.config import Settings
from app.ingestion.chunking import Chunk
from app.ingestion.embedding import SparseVec

_NAMESPACE = uuid.UUID("6f1c3a52-0c0e-4b6a-9d36-5d0a6a0f1e11")
_UPSERT_BATCH = 64


def collection_name(settings: Settings) -> str:
    return settings.collection_name(settings.qdrant_chunks_collection)


def point_id(document_id: uuid.UUID, version: int, chunk_index: int) -> str:
    return str(uuid.uuid5(_NAMESPACE, f"{document_id}:{version}:{chunk_index}"))


def ensure_collection(client: QdrantClient, settings: Settings) -> None:
    name = collection_name(settings)
    if client.collection_exists(name):
        return
    try:
        client.create_collection(
            name,
            vectors_config={
                "dense": VectorParams(size=settings.embedding_dim, distance=Distance.COSINE)
            },
            sparse_vectors_config={"sparse": SparseVectorParams(modifier=Modifier.IDF)},
        )
    except UnexpectedResponse as exc:
        if exc.status_code != 409:  # another worker created it first: fine
            raise
    client.create_payload_index(
        name,
        "user_id",
        field_schema=KeywordIndexParams(type=KeywordIndexType.KEYWORD, is_tenant=True),
    )
    client.create_payload_index(name, "document_id", field_schema=PayloadSchemaType.KEYWORD)


def _owner_filter(user_id: uuid.UUID, document_id: uuid.UUID) -> Filter:
    # user_id is always part of the filter, even for internal maintenance operations.
    return Filter(
        must=[
            FieldCondition(key="user_id", match=MatchValue(value=str(user_id))),
            FieldCondition(key="document_id", match=MatchValue(value=str(document_id))),
        ]
    )


def upsert_chunks(
    client: QdrantClient,
    settings: Settings,
    *,
    user_id: uuid.UUID,
    document_id: uuid.UUID,
    version: int,
    filename: str,
    chunks: Sequence[Chunk],
    dense: Sequence[Sequence[float]],
    sparse: Sequence[SparseVec],
    embedding_model: str,
) -> int:
    ensure_collection(client, settings)
    timestamp = datetime.now(UTC).isoformat()
    points: list[PointStruct] = []
    for chunk, dvec, svec in zip(chunks, dense, sparse, strict=True):
        pid = point_id(document_id, version, chunk.index)
        payload: dict[str, Any] = {
            "user_id": str(user_id),
            "document_id": str(document_id),
            "document_version": version,
            "chunk_id": pid,
            "chunk_index": chunk.index,
            "filename": filename,
            "page_number": chunk.page_number,
            "page_end": chunk.page_end,
            "section_path": chunk.section_path,
            "tokens": chunk.tokens,
            "heading": chunk.heading,
            "ingestion_timestamp": timestamp,
            "embedding_model_version": embedding_model,
            "text": chunk.text,
        }
        points.append(
            PointStruct(
                id=pid,
                vector={
                    "dense": list(dvec),
                    "sparse": SparseVector(indices=svec.indices, values=svec.values),
                },
                payload=payload,
            )
        )
    name = collection_name(settings)
    for start in range(0, len(points), _UPSERT_BATCH):
        client.upsert(name, points=points[start : start + _UPSERT_BATCH], wait=True)
    return len(points)


def delete_stale_versions(
    client: QdrantClient,
    settings: Settings,
    *,
    user_id: uuid.UUID,
    document_id: uuid.UUID,
    keep: int,
) -> None:
    """Remove points of this document from older versions (after the new version is indexed)."""
    flt = Filter(
        must=[
            FieldCondition(key="user_id", match=MatchValue(value=str(user_id))),
            FieldCondition(key="document_id", match=MatchValue(value=str(document_id))),
        ],
        must_not=[FieldCondition(key="document_version", match=MatchValue(value=keep))],
    )
    _delete(client, settings, flt)


def delete_document_points(
    client: QdrantClient, settings: Settings, *, user_id: uuid.UUID, document_id: uuid.UUID
) -> None:
    _delete(client, settings, _owner_filter(user_id, document_id))


def _delete(client: QdrantClient, settings: Settings, flt: Filter) -> None:
    name = collection_name(settings)
    if not client.collection_exists(name):
        return
    client.delete(name, points_selector=FilterSelector(filter=flt), wait=True)


def count_document_points(
    client: QdrantClient, settings: Settings, *, user_id: uuid.UUID, document_id: uuid.UUID
) -> int:
    name = collection_name(settings)
    if not client.collection_exists(name):
        return 0
    return client.count(name, count_filter=_owner_filter(user_id, document_id), exact=True).count
