"""Helpers for memory tests."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import httpx2
from qdrant_client import QdrantClient

from app.core.config import get_settings
from app.memory.index import collection, count_vectors
from tests.integration.docs_helpers import API


def vector_count(user_id: uuid.UUID) -> int:
    settings = get_settings()
    client = QdrantClient(url=settings.qdrant_url, timeout=30)
    try:
        return count_vectors(client, settings, user_id=user_id)
    finally:
        client.close()


def vector_ids(user_id: uuid.UUID) -> set[str]:
    from qdrant_client.models import FieldCondition, Filter, MatchValue

    settings = get_settings()
    client = QdrantClient(url=settings.qdrant_url, timeout=30)
    try:
        if not client.collection_exists(collection(settings)):
            return set()
        points, _ = client.scroll(
            collection(settings),
            scroll_filter=Filter(
                must=[FieldCondition(key="user_id", match=MatchValue(value=str(user_id)))]
            ),
            limit=1000,
            with_payload=False,
        )
        return {str(p.id) for p in points}
    finally:
        client.close()


async def eventually(check: Callable[[], Awaitable[bool] | bool], *, wait_s: float = 30.0) -> None:
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        result = check()
        if (await result) if isinstance(result, Awaitable) else result:
            return
        await asyncio.sleep(0.2)
    raise AssertionError("condition not reached in time")


async def user_id_of(client: httpx2.AsyncClient, headers: dict[str, str]) -> uuid.UUID:
    resp = await client.get(f"{API}/auth/me", headers=headers)
    assert resp.status_code == 200, resp.text
    return uuid.UUID(resp.json()["id"])


async def add_memory(
    client: httpx2.AsyncClient, headers: dict[str, str], content: str, **extra: Any
) -> dict[str, Any]:
    resp = await client.post(f"{API}/memory", json={"content": content, **extra}, headers=headers)
    assert resp.status_code == 201, resp.text
    return dict(resp.json())
