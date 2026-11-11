"""Integration fixtures: migrated rag_test database, real Redis (db 15), ASGI client."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from pathlib import Path
from typing import cast

import httpx2
import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from celery.contrib.testing.worker import start_worker
from fastapi import FastAPI
from sqlalchemy import text

from app.core.config import Settings, get_settings
from app.infra.container import Infrastructure
from app.ingestion.embedding import set_embedder_factory
from app.ingestion.runtime import WorkerRuntime, set_runtime
from app.main import create_app
from app.tasks import ingestion as ingestion_tasks
from app.tasks.celery_app import celery_app
from tests.fakes import HashEmbedder
from tests.helpers import delete_keys

ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"

ClientFactory = Callable[[Settings], Awaitable[httpx2.AsyncClient]]


@pytest.fixture(scope="session", autouse=True)
def _migrated_database() -> None:
    command.upgrade(Config(str(ALEMBIC_INI)), "head")


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def infra() -> AsyncIterator[Infrastructure]:
    value = Infrastructure.create(get_settings())
    yield value
    await value.close()


@pytest_asyncio.fixture(autouse=True, loop_scope="session")
async def _clean_state(infra: Infrastructure) -> None:
    """Fresh users/tokens and rate-limit counters for every test."""
    async with infra.session_factory() as session:
        await session.execute(text("TRUNCATE users CASCADE"))
        await session.commit()
    await delete_keys(infra.redis, "rl:*")
    await delete_keys(infra.redis, "llmc:*")
    for pattern in ("celery", "unacked*", "_kombu*"):  # leftovers from earlier tests' broker use
        await delete_keys(infra.redis, pattern)
    async with infra.session_factory() as session:
        await session.execute(text("TRUNCATE outbox_events"))
        await session.commit()
    async with infra.session_factory() as session:  # checkpoint tables exist once a graph has run
        await session.execute(
            text(
                "DO $$ BEGIN IF to_regclass('public.checkpoints') IS NOT NULL THEN "
                "TRUNCATE checkpoints, checkpoint_blobs, checkpoint_writes; END IF; END $$"
            )
        )
        await session.commit()
    _reset_vectors_and_objects()
    for eml in get_settings().email_file_dir.glob("*.eml"):
        eml.unlink()


def _reset_vectors_and_objects() -> None:
    """Fresh Qdrant collection and object store for every test."""
    import shutil

    from qdrant_client import QdrantClient

    from app.ingestion.indexing import collection_name
    from app.memory.index import collection as memory_collection

    settings = get_settings()
    client = QdrantClient(url=settings.qdrant_url, timeout=30)
    try:
        for name in (collection_name(settings), memory_collection(settings)):
            if client.collection_exists(name):
                client.delete_collection(name)
    finally:
        client.close()
    root = settings.storage_local_root
    if root.exists():
        for child in root.iterdir():
            shutil.rmtree(child, ignore_errors=True) if child.is_dir() else child.unlink()
        (root / ".tmp").mkdir(exist_ok=True)


def build_app(settings: Settings, infra: Infrastructure) -> FastAPI:
    app = create_app(settings)
    app.state.infra = infra  # lifespan is not run by ASGITransport; share the session infra
    return app


@pytest_asyncio.fixture(loop_scope="session")
async def app(infra: Infrastructure) -> FastAPI:
    return build_app(get_settings(), infra)


@pytest_asyncio.fixture(loop_scope="session")
async def client(app: FastAPI) -> AsyncIterator[httpx2.AsyncClient]:
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture(loop_scope="session")
async def client_with_settings(
    infra: Infrastructure,
) -> AsyncIterator[ClientFactory]:
    """Factory for a client whose app uses modified settings (e.g. tight rate limits)."""
    opened: list[httpx2.AsyncClient] = []
    extra: list[Infrastructure] = []

    async def factory(settings: Settings) -> httpx2.AsyncClient:
        own = Infrastructure.create(settings)
        extra.append(own)
        transport = httpx2.ASGITransport(app=build_app(settings, own))
        c = httpx2.AsyncClient(transport=transport, base_url="http://test")
        opened.append(c)
        return c

    yield factory
    for c in opened:
        await c.aclose()
    for own in extra:
        await own.close()


# ---------------------------------------------------------------- real Celery worker (opt-in)


REGISTERED_TASKS = (ingestion_tasks.process_document,)  # importing the module registers the tasks


@pytest.fixture(scope="module")
def runtime() -> Iterator[WorkerRuntime]:
    """A real Celery worker (thread, solo pool) against real Redis/PostgreSQL/Qdrant/storage.

    Only the embedding model is replaced by the deterministic test-only HashEmbedder.
    """
    set_embedder_factory(HashEmbedder)
    rt = WorkerRuntime.create(get_settings())
    set_runtime(rt)
    with start_worker(celery_app, pool="solo", perform_ping_check=False, shutdown_timeout=30):
        yield rt
    set_runtime(None)
    rt.close()
    set_embedder_factory(None)


@pytest.fixture
def embedder(runtime: WorkerRuntime) -> HashEmbedder:
    emb = cast(HashEmbedder, runtime.embedder)
    emb.fail_next = 0
    emb.on_dense = None
    emb.calls = 0
    return emb
