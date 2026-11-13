"""Engine and session factories: async engine for the API, sync engine for Celery and Alembic."""

from __future__ import annotations

from sqlalchemy import Engine, create_engine
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.core.telemetry import instrument_engine


def create_async_db_engine(settings: Settings) -> AsyncEngine:
    engine = create_async_engine(
        settings.database_url(driver="asyncpg"),
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout_s,
        pool_recycle=settings.db_pool_recycle_s,
        pool_pre_ping=True,
        connect_args={"timeout": settings.db_connect_timeout_s},
    )
    instrument_engine(engine)
    return engine


def create_sync_db_engine(settings: Settings) -> Engine:
    """Used by Celery tasks and Alembic (see ADR-003: sync tasks + sync clients)."""
    engine = create_engine(
        settings.database_url(driver="psycopg"),
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout_s,
        pool_recycle=settings.db_pool_recycle_s,
        pool_pre_ping=True,
        connect_args={"connect_timeout": settings.db_connect_timeout_s},
    )
    instrument_engine(engine)
    return engine


def async_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


def sync_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)
