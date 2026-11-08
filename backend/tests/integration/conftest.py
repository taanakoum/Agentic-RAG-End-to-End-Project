"""Integration fixtures: migrated rag_test database, real Redis (db 15), ASGI client."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import httpx2
import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from sqlalchemy import text

from app.core.config import Settings, get_settings
from app.infra.container import Infrastructure
from app.main import create_app
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
