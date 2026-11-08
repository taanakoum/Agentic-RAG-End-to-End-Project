from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.pool import QueuePool

from app.core.config import get_settings
from app.database.session import (
    async_session_factory,
    create_async_db_engine,
    create_sync_db_engine,
)

pytestmark = pytest.mark.integration

ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"


def _cfg() -> Config:
    return Config(str(ALEMBIC_INI))


def _current_version() -> str | None:
    engine = create_sync_db_engine(get_settings())
    try:
        with engine.connect() as conn:
            exists = conn.execute(text("SELECT to_regclass('public.alembic_version')")).scalar()
            if exists is None:
                return None
            return conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
    finally:
        engine.dispose()


def test_uses_test_database() -> None:
    assert get_settings().postgres_db == "rag_test"


def test_sync_connection_works() -> None:
    engine = create_sync_db_engine(get_settings())
    try:
        with engine.connect() as conn:
            assert conn.execute(text("SELECT 1")).scalar() == 1
            assert conn.execute(text("SELECT current_database()")).scalar() == "rag_test"
    finally:
        engine.dispose()


async def test_async_connection_and_pool() -> None:
    engine = create_async_db_engine(get_settings())
    try:
        factory = async_session_factory(engine)
        async with factory() as session:
            assert (await session.execute(text("SELECT 1"))).scalar() == 1
        assert isinstance(engine.pool, QueuePool)
        assert engine.pool.size() == get_settings().db_pool_size
    finally:
        await engine.dispose()


def test_migrations_upgrade_downgrade_upgrade() -> None:
    command.downgrade(_cfg(), "base")
    assert _current_version() is None or _current_version() == ""
    command.upgrade(_cfg(), "head")
    assert _current_version() == "0002"
    command.downgrade(_cfg(), "base")
    command.upgrade(_cfg(), "head")
    assert _current_version() == "0002"
