"""Redis-backed fixed-window rate limiting. Fails closed: if Redis is down, callers get 503."""

from __future__ import annotations

import hashlib

from redis.asyncio import Redis as AsyncRedis
from redis.exceptions import RedisError

from app.core.exceptions import ExternalServiceUnavailableError, RateLimitExceededError
from app.core.logging import get_logger

log = get_logger(__name__)

# Atomic INCR + EXPIRE (never leaves a counter without a TTL). Returns {count, ttl_seconds}.
_HIT_SCRIPT = """
local c = redis.call('INCR', KEYS[1])
if c == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
local ttl = redis.call('TTL', KEYS[1])
if ttl < 0 then redis.call('EXPIRE', KEYS[1], ARGV[1]); ttl = tonumber(ARGV[1]) end
return {c, ttl}
"""


def subject_key(*parts: str) -> str:
    """Stable, PII-free Redis key: parts are hashed (emails and IPs never stored in clear)."""
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:32]


class RateLimiter:
    def __init__(self, redis: AsyncRedis) -> None:
        self._redis = redis

    async def _hit(self, key: str, window_s: int) -> tuple[int, int]:
        try:
            # redis-py types eval() as Awaitable[str] | str; the script returns a 2-element list.
            result = await self._redis.eval(_HIT_SCRIPT, 1, key, window_s)  # pyright: ignore[reportGeneralTypeIssues, reportUnknownMemberType]
        except RedisError as exc:
            log.error("rate_limiter_unavailable", error=type(exc).__name__)
            raise ExternalServiceUnavailableError() from exc
        return int(result[0]), int(result[1])

    async def hit(self, bucket: str, subject: str, *, limit: int, window_s: int) -> None:
        """Count one event; raise RateLimitExceededError once `limit` is exceeded in the window."""
        count, ttl = await self._hit(f"rl:{bucket}:{subject}", window_s)
        if count > limit:
            raise RateLimitExceededError(ttl)

    async def check(self, bucket: str, subject: str, *, limit: int) -> None:
        """Raise if the counter has already reached `limit` (does not count)."""
        key = f"rl:{bucket}:{subject}"
        try:
            raw = await self._redis.get(key)
            ttl = await self._redis.ttl(key) if raw is not None else 0
        except RedisError as exc:
            log.error("rate_limiter_unavailable", error=type(exc).__name__)
            raise ExternalServiceUnavailableError() from exc
        if raw is not None and int(raw) >= limit:
            raise RateLimitExceededError(int(ttl))

    async def fail(self, bucket: str, subject: str, *, window_s: int) -> None:
        """Record a failure without enforcing (enforcement happens in `check`)."""
        await self._hit(f"rl:{bucket}:{subject}", window_s)

    async def reset(self, bucket: str, subject: str) -> None:
        try:
            await self._redis.delete(f"rl:{bucket}:{subject}")
        except RedisError as exc:
            log.error("rate_limiter_unavailable", error=type(exc).__name__)
            raise ExternalServiceUnavailableError() from exc
