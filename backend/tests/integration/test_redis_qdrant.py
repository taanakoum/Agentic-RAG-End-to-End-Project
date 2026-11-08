from __future__ import annotations

import uuid

import pytest
from qdrant_client.models import Distance, PointStruct, VectorParams

from app.core.config import get_settings
from app.infra.clients import (
    async_redis_ping,
    create_async_qdrant,
    create_async_redis,
    create_sync_qdrant,
    create_sync_redis,
    sync_redis_ping,
)

pytestmark = pytest.mark.integration


def test_sync_redis_roundtrip_uses_test_db() -> None:
    client = create_sync_redis(get_settings())
    try:
        key = f"test:{uuid.uuid4().hex}"
        assert sync_redis_ping(client)
        client.set(key, "v", ex=30)
        assert client.get(key) == b"v"
        assert client.connection_pool.connection_kwargs["db"] == 15
        client.delete(key)
    finally:
        client.close()


async def test_async_redis_roundtrip() -> None:
    client = create_async_redis(get_settings())
    try:
        assert await async_redis_ping(client)
        key = f"test:{uuid.uuid4().hex}"
        await client.set(key, "v", ex=30)
        assert await client.get(key) == b"v"
        await client.delete(key)
    finally:
        await client.aclose()


def test_sync_qdrant_collection_lifecycle() -> None:
    settings = get_settings()
    client = create_sync_qdrant(settings)
    name = settings.collection_name(f"foundation_{uuid.uuid4().hex[:8]}")
    assert name.startswith("test_")
    try:
        client.create_collection(
            name, vectors_config=VectorParams(size=4, distance=Distance.COSINE)
        )
        client.upsert(
            name, points=[PointStruct(id=1, vector=[0.1, 0.2, 0.3, 0.4], payload={"u": "a"})]
        )
        found = client.retrieve(name, ids=[1])
        assert found[0].payload == {"u": "a"}
    finally:
        client.delete_collection(name)
        client.close()


async def test_async_qdrant_reachable() -> None:
    client = create_async_qdrant(get_settings())
    try:
        result = await client.get_collections()
        assert result.collections is not None
    finally:
        await client.close()
