"""Typed wrappers around third-party calls that lack complete annotations (tests only)."""

from __future__ import annotations

import re
from email import message_from_bytes, policy
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import jwt
from redis.asyncio import Redis as AsyncRedis


def unverified_claims(token: str) -> dict[str, Any]:
    """Decode a JWT WITHOUT verifying it (to read claims in assertions)."""
    return jwt.decode(token, options={"verify_signature": False})  # pyright: ignore[reportUnknownMemberType]


def unverified_header(token: str) -> dict[str, Any]:
    return jwt.get_unverified_header(token)


def forge_jwt(payload: dict[str, Any], key: str, algorithm: str) -> str:
    """Sign arbitrary claims, used to build deliberately invalid tokens."""
    return jwt.encode(payload, key, algorithm=algorithm)  # pyright: ignore[reportUnknownMemberType]


async def delete_keys(redis: AsyncRedis, pattern: str) -> None:
    keys: list[bytes] = [k async for k in redis.scan_iter(match=pattern)]  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]
    if keys:
        await redis.delete(*keys)


def read_outbox(directory: Path) -> list[EmailMessage]:
    """Parse every .eml the file email backend has written, oldest first."""
    files = sorted(directory.glob("*.eml"), key=lambda p: p.stat().st_mtime_ns)
    return [message_from_bytes(f.read_bytes(), policy=policy.default) for f in files]


def token_from(message: EmailMessage, label: str) -> str:
    """Extract the single-use token from an email body line like 'Reset token: <token>'."""
    match = re.search(rf"{re.escape(label)}: (\S+)", message.get_content())
    assert match is not None, "token line not found in email"
    return match.group(1)
