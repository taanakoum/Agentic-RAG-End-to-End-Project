"""Security primitives: email normalization, password hashing (Argon2id), JWT, opaque tokens."""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from email_validator import EmailNotValidError, validate_email

from app.core.config import Settings

# ---------------------------------------------------------------- email


class InvalidEmailError(ValueError):
    """The address is not a syntactically valid email."""


def normalize_email(raw: str) -> str:
    """The single shared normalization used for uniqueness and lookup.

    Validates syntax (no DNS lookups), applies Unicode normalization, and lowercases.
    """
    try:
        info = validate_email(raw.strip(), check_deliverability=False)
    except EmailNotValidError as exc:
        raise InvalidEmailError(str(exc)) from exc
    return unicodedata.normalize("NFKC", info.normalized).lower()


# ---------------------------------------------------------------- passwords

_COMMON_PASSWORDS = frozenset(
    {
        "password1234",
        "123456789012",
        "qwertyuiop12",
        "iloveyou1234",
        "letmein12345",
        "administrator",
        "passwordpassword",
        "1234567890123",
        "welcome12345",
    }
)


def password_policy_violations(password: str, *, email: str, settings: Settings) -> list[str]:
    """Return policy violation codes (empty list = acceptable)."""
    problems: list[str] = []
    if len(password) < settings.password_min_length:
        problems.append("password_too_short")
    if len(password) > settings.password_max_length:
        problems.append("password_too_long")
    if not password.strip():
        problems.append("password_blank")
    if len(set(password)) < 5:
        problems.append("password_too_repetitive")
    lowered = password.lower()
    local = email.split("@", 1)[0].lower()
    if lowered in _COMMON_PASSWORDS or (len(local) >= 4 and local in lowered):
        problems.append("password_too_guessable")
    return problems


class PasswordService:
    """Argon2id hashing. CPU/memory heavy, so it runs in worker threads behind a semaphore."""

    def __init__(self, settings: Settings) -> None:
        self._hasher = PasswordHasher(
            time_cost=settings.argon2_time_cost,
            memory_cost=settings.argon2_memory_kib,
            parallelism=settings.argon2_parallelism,
            type=Type.ID,
        )
        self._semaphore = asyncio.Semaphore(settings.max_concurrent_hashes)
        # Verified against when the account does not exist, to equalize timing.
        self._dummy_hash = self._hasher.hash(secrets.token_urlsafe(16))

    def hash_sync(self, password: str) -> str:
        return self._hasher.hash(password)

    def verify_sync(self, password_hash: str, password: str) -> bool:
        try:
            return self._hasher.verify(password_hash, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False

    def needs_rehash(self, password_hash: str) -> bool:
        return self._hasher.check_needs_rehash(password_hash)

    async def hash(self, password: str) -> str:
        async with self._semaphore:
            return await asyncio.to_thread(self.hash_sync, password)

    async def verify(self, password_hash: str, password: str) -> bool:
        async with self._semaphore:
            return await asyncio.to_thread(self.verify_sync, password_hash, password)

    async def verify_dummy(self, password: str) -> None:
        """Burn comparable time for unknown accounts (always False)."""
        await self.verify(self._dummy_hash, password)


# ---------------------------------------------------------------- opaque (refresh) tokens


def generate_opaque_token() -> str:
    return secrets.token_urlsafe(48)


def hash_opaque_token(token: str) -> str:
    """SHA-256 is appropriate here: the token is 384 bits of randomness, not a password."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- JWT

_ALGORITHM = "HS256"
_REQUIRED_CLAIMS = ["exp", "iat", "iss", "aud", "sub", "type", "sid", "jti"]


class TokenError(Exception):
    """The JWT is missing, malformed, expired, or otherwise invalid."""


@dataclass(frozen=True, slots=True)
class AccessClaims:
    user_id: uuid.UUID
    session_id: uuid.UUID
    expires_at: datetime


def create_access_token(
    settings: Settings,
    *,
    user_id: uuid.UUID,
    session_id: uuid.UUID,
    now: datetime | None = None,
    ttl: timedelta | None = None,
    claim_type: str = "access",
) -> str:
    issued = now or datetime.now(UTC)
    expires = issued + (ttl if ttl is not None else timedelta(seconds=settings.access_token_ttl_s))
    payload: dict[str, Any] = {
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "sub": str(user_id),
        "sid": str(session_id),
        "type": claim_type,
        "jti": uuid.uuid4().hex,
        "iat": int(issued.timestamp()),
        "exp": int(expires.timestamp()),
    }
    # PyJWT's key type aliases resolve to Unknown without the optional `cryptography` extra.
    return jwt.encode(  # pyright: ignore[reportUnknownMemberType]
        payload, settings.jwt_secret.get_secret_value(), algorithm=_ALGORITHM
    )


def decode_access_token(settings: Settings, token: str) -> AccessClaims:
    """Validate signature (HS256 only), exp/iat/iss/aud, required claims and token type."""
    try:
        payload: dict[str, Any] = jwt.decode(  # pyright: ignore[reportUnknownMemberType]
            token,
            settings.jwt_secret.get_secret_value(),
            algorithms=[_ALGORITHM],
            issuer=settings.jwt_issuer,
            audience=settings.jwt_audience,
            options={"require": _REQUIRED_CLAIMS, "verify_iat": True},
        )
        if payload["type"] != "access":
            raise TokenError("wrong token type")
        return AccessClaims(
            user_id=uuid.UUID(str(payload["sub"])),
            session_id=uuid.UUID(str(payload["sid"])),
            expires_at=datetime.fromtimestamp(int(payload["exp"]), UTC),
        )
    except TokenError:
        raise
    except (jwt.PyJWTError, ValueError, TypeError, KeyError) as exc:
        raise TokenError("invalid token") from exc
