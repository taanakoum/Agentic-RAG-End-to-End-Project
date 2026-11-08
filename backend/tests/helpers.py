"""Typed wrappers around third-party calls that lack complete annotations (tests only)."""

from __future__ import annotations

from typing import Any

import jwt
from redis.asyncio import Redis as AsyncRedis


def unverified_claims(token: str) -> dict[str, Any]:
    """Decode a JWT WITHOUT verifying it (to read claims in assertions)."""
    return jwt.decode(token, options={"verify_signature": False})  # pyright: ignore[reportUnknownMemberType]


def forge_jwt(payload: dict[str, Any], key: str, algorithm: str) -> str:
    """Sign arbitrary claims, used to build deliberately invalid tokens."""
    return jwt.encode(payload, key, algorithm=algorithm)  # pyright: ignore[reportUnknownMemberType]


async def delete_keys(redis: AsyncRedis, pattern: str) -> None:
    keys: list[bytes] = [k async for k in redis.scan_iter(match=pattern)]  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]
    if keys:
        await redis.delete(*keys)
