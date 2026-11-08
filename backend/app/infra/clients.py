"""Factories for Redis and Qdrant clients. Lifecycle (creation/closing) is owned by the caller."""

from __future__ import annotations

from qdrant_client import AsyncQdrantClient, QdrantClient
from redis import Redis
from redis.asyncio import Redis as AsyncRedis

from app.core.config import Settings


# redis-py ships partially-unannotated signatures (from_url, ping(**kwargs)); the narrow ignores
# below are confined to this module so the rest of the code base sees fully typed helpers.
def create_async_redis(settings: Settings) -> AsyncRedis:
    return AsyncRedis.from_url(  # pyright: ignore[reportUnknownMemberType]
        settings.redis_url,
        socket_timeout=settings.redis_socket_timeout_s,
        socket_connect_timeout=settings.redis_socket_timeout_s,
        health_check_interval=30,
    )


def create_sync_redis(settings: Settings) -> Redis:
    return Redis.from_url(  # pyright: ignore[reportUnknownMemberType]
        settings.redis_url,
        socket_timeout=settings.redis_socket_timeout_s,
        socket_connect_timeout=settings.redis_socket_timeout_s,
        health_check_interval=30,
    )


def _qdrant_key(settings: Settings) -> str | None:
    return settings.qdrant_api_key.get_secret_value() if settings.qdrant_api_key else None


def create_async_qdrant(settings: Settings) -> AsyncQdrantClient:
    return AsyncQdrantClient(
        url=settings.qdrant_url,
        api_key=_qdrant_key(settings),
        timeout=int(settings.qdrant_timeout_s),
    )


def create_sync_qdrant(settings: Settings) -> QdrantClient:
    return QdrantClient(
        url=settings.qdrant_url,
        api_key=_qdrant_key(settings),
        timeout=int(settings.qdrant_timeout_s),
    )


async def async_redis_ping(client: AsyncRedis) -> bool:
    return bool(await client.ping())  # pyright: ignore[reportUnknownMemberType]


def sync_redis_ping(client: Redis) -> bool:
    return bool(client.ping())  # pyright: ignore[reportUnknownMemberType]
