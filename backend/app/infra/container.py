"""Process-wide shared infrastructure for the API. Created once in the lifespan, closed on shutdown.

Holds only connections/pools (no per-user mutable state), so the API stays stateless and scalable.
"""

from __future__ import annotations

from dataclasses import dataclass

from qdrant_client import AsyncQdrantClient
from redis.asyncio import Redis as AsyncRedis
from sqlalchemy import Engine
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.core.security import PasswordService
from app.database.session import (
    async_session_factory,
    create_async_db_engine,
    create_sync_db_engine,
    sync_session_factory,
)
from app.infra.clients import create_async_qdrant, create_async_redis
from app.infra.email import EmailSender, create_email_sender
from app.infra.rate_limit import RateLimiter
from app.storage.base import ObjectStorage
from app.storage.factory import create_storage


@dataclass(slots=True)
class Infrastructure:
    settings: Settings
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    redis: AsyncRedis
    qdrant: AsyncQdrantClient
    storage: ObjectStorage
    passwords: PasswordService
    rate_limiter: RateLimiter
    email: EmailSender
    sync_engine: Engine
    sync_session_factory: sessionmaker[Session]

    @classmethod
    def create(cls, settings: Settings) -> Infrastructure:
        engine = create_async_db_engine(settings)
        redis = create_async_redis(settings)
        sync_engine = create_sync_db_engine(settings)
        return cls(
            settings=settings,
            engine=engine,
            session_factory=async_session_factory(engine),
            redis=redis,
            qdrant=create_async_qdrant(settings),
            storage=create_storage(settings),
            passwords=PasswordService(settings),
            rate_limiter=RateLimiter(redis),
            email=create_email_sender(settings),
            sync_engine=sync_engine,
            sync_session_factory=sync_session_factory(sync_engine),
        )

    async def close(self) -> None:
        await self.engine.dispose()
        self.sync_engine.dispose()
        await self.redis.aclose()
        await self.qdrant.close()
