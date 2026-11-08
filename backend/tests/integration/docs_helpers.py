"""Shared helpers for document/ingestion integration tests."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx2
from qdrant_client import QdrantClient
from sqlalchemy import text

from app.core.config import get_settings
from app.infra.container import Infrastructure
from app.ingestion.indexing import collection_name

PASSWORD = "correct-horse-battery-staple"
API = "/api/v1"


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def new_user(client: httpx2.AsyncClient, email: str = "alice@example.com") -> dict[str, str]:
    reg = await client.post(f"{API}/auth/register", json={"email": email, "password": PASSWORD})
    assert reg.status_code == 201, reg.text
    login = await client.post(f"{API}/auth/login", json={"email": email, "password": PASSWORD})
    assert login.status_code == 200, login.text
    return bearer(login.json()["access_token"])


async def upload(
    client: httpx2.AsyncClient,
    headers: dict[str, str],
    data: bytes,
    *,
    filename: str = "report.pdf",
    content_type: str | None = "application/pdf",
) -> httpx2.Response:
    files = {"file": (filename, data, content_type)} if content_type else {"file": (filename, data)}
    return await client.post(f"{API}/documents/upload", files=files, headers=headers)


async def wait_for_status(
    client: httpx2.AsyncClient,
    headers: dict[str, str],
    document_id: str,
    wanted: set[str],
    *,
    wait_s: float = 90.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + wait_s
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        resp = await client.get(f"{API}/documents/{document_id}", headers=headers)
        assert resp.status_code == 200, resp.text
        last = resp.json()
        if last["status"] in wanted:
            return last
        await asyncio.sleep(0.25)
    raise AssertionError(f"document stuck in {last.get('status')!r}, wanted {wanted}: {last}")


async def scalar(infra: Infrastructure, sql: str, **params: Any) -> Any:
    async with infra.session_factory() as session:
        return (await session.execute(text(sql), params)).scalar()


def stored_object_count() -> int:
    root = get_settings().storage_local_root
    return sum(1 for p in root.rglob("*") if p.is_file() and ".tmp" not in p.parts)


def points_for(document_id: str | None = None, user_id: str | None = None) -> list[Any]:
    """All Qdrant points (optionally filtered) with payloads and vectors, via the sync client."""
    from qdrant_client.models import Condition, FieldCondition, Filter, MatchValue

    settings = get_settings()
    client = QdrantClient(url=settings.qdrant_url, timeout=30)
    try:
        name = collection_name(settings)
        if not client.collection_exists(name):
            return []
        must: list[Condition] = []
        if document_id:
            must.append(FieldCondition(key="document_id", match=MatchValue(value=document_id)))
        if user_id:
            must.append(FieldCondition(key="user_id", match=MatchValue(value=user_id)))
        points, _ = client.scroll(
            name,
            scroll_filter=Filter(must=must) if must else None,
            limit=1000,
            with_payload=True,
            with_vectors=True,
        )
        return list(points)
    finally:
        client.close()
