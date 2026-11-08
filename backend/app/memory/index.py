"""Qdrant side of memory: one dense vector per memory, point id = memory id, tenant = user_id.

Sync functions are used by Celery tasks; `search_vectors` is the async query path of the API.
EVERY call filters on user_id (a memory id alone never selects a point).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from qdrant_client import AsyncQdrantClient, QdrantClient
from qdrant_client.http.exceptions import UnexpectedResponse
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    FilterSelector,
    HasIdCondition,
    KeywordIndexParams,
    KeywordIndexType,
    MatchValue,
    PointStruct,
    VectorParams,
)

from app.core.config import Settings


def collection(settings: Settings) -> str:
    return settings.collection_name(settings.qdrant_memories_collection)


def _user_filter(user_id: uuid.UUID, memory_id: uuid.UUID | None = None) -> Filter:
    must: list[FieldCondition | HasIdCondition] = [
        FieldCondition(key="user_id", match=MatchValue(value=str(user_id)))
    ]
    if memory_id is not None:
        must.append(HasIdCondition(has_id=[str(memory_id)]))
    return Filter(must=list(must))


def ensure_collection(client: QdrantClient, settings: Settings) -> None:
    name = collection(settings)
    if client.collection_exists(name):
        return
    try:
        client.create_collection(
            name,
            vectors_config=VectorParams(size=settings.embedding_dim, distance=Distance.COSINE),
        )
    except UnexpectedResponse as exc:
        if exc.status_code != 409:  # created by another worker meanwhile
            raise
    client.create_payload_index(
        name,
        "user_id",
        field_schema=KeywordIndexParams(type=KeywordIndexType.KEYWORD, is_tenant=True),
    )


def upsert_vector(
    client: QdrantClient,
    settings: Settings,
    *,
    memory_id: uuid.UUID,
    user_id: uuid.UUID,
    kind: str,
    vector: Sequence[float],
    embedding_model: str,
) -> None:
    ensure_collection(client, settings)
    client.upsert(
        collection(settings),
        points=[
            PointStruct(
                id=str(memory_id),
                vector=list(vector),
                payload={
                    "user_id": str(user_id),
                    "memory_id": str(memory_id),
                    "kind": kind,
                    "embedding_model_version": embedding_model,
                },
            )
        ],
        wait=True,
    )


def _delete(client: QdrantClient, settings: Settings, flt: Filter) -> None:
    name = collection(settings)
    if client.collection_exists(name):
        client.delete(name, points_selector=FilterSelector(filter=flt), wait=True)


def delete_vector(
    client: QdrantClient, settings: Settings, *, user_id: uuid.UUID, memory_id: uuid.UUID
) -> None:
    _delete(client, settings, _user_filter(user_id, memory_id))


def delete_user_vectors(client: QdrantClient, settings: Settings, *, user_id: uuid.UUID) -> None:
    _delete(client, settings, _user_filter(user_id))


def count_vectors(client: QdrantClient, settings: Settings, *, user_id: uuid.UUID) -> int:
    name = collection(settings)
    if not client.collection_exists(name):
        return 0
    return client.count(name, count_filter=_user_filter(user_id), exact=True).count


async def search_vectors(
    qdrant: AsyncQdrantClient,
    settings: Settings,
    *,
    user_id: uuid.UUID,
    vector: Sequence[float],
    limit: int,
    min_score: float,
) -> list[tuple[uuid.UUID, float]]:
    """(memory_id, cosine score) for this user's closest memories above `min_score`."""
    if not await qdrant.collection_exists(collection(settings)):
        return []
    response = await qdrant.query_points(
        collection(settings),
        query=list(vector),
        query_filter=_user_filter(user_id),
        limit=limit,
        score_threshold=min_score,
        with_payload=["user_id", "memory_id"],
        timeout=max(1, int(settings.qdrant_timeout_s)),
    )
    out: list[tuple[uuid.UUID, float]] = []
    for point in response.points:
        payload = point.payload or {}
        if payload.get("user_id") != str(user_id):  # defence in depth; the filter guarantees it
            continue
        out.append((uuid.UUID(str(payload["memory_id"])), float(point.score)))
    return out
