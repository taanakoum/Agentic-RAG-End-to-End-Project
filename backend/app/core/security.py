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

from app.core.breached import is_breached_password
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


PASSWORD_MESSAGES = {
    "password_too_short": "Password is too short.",
    "password_too_long": "Password is too long.",
    "password_blank": "Password must not be blank.",
    "password_too_repetitive": "Password is too repetitive.",
    "password_too_guessable": "Password is too easy to guess.",
    "password_breached": "This password appears in lists of commonly used or breached passwords.",
}


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
    if is_breached_password(password):
        problems.append("password_breached")
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


# ---------------------------------------------------------------- JWT (with key rotation)

_ALGORITHM = "HS256"
_ACCESS_CLAIMS = ["exp", "iat", "iss", "aud", "sub", "type", "sid", "jti"]
_MFA_CLAIMS = ["exp", "iat", "iss", "aud", "sub", "type", "jti"]


class TokenError(Exception):
    """The JWT is missing, malformed, expired, or otherwise invalid."""


@dataclass(frozen=True, slots=True)
class AccessClaims:
    user_id: uuid.UUID
    session_id: uuid.UUID
    expires_at: datetime


def _signing_keys(settings: Settings) -> dict[str, str]:
    """kid -> secret for every key that may VERIFY tokens (active first, then retired)."""
    keys = {settings.jwt_key_id: settings.jwt_secret.get_secret_value()}
    for kid, secret in settings.jwt_retired_keys.items():
        keys.setdefault(kid, secret.get_secret_value())
    return keys


def _encode(settings: Settings, payload: dict[str, Any]) -> str:
    # PyJWT's key type aliases resolve to Unknown without the optional `cryptography` extra.
    return jwt.encode(  # pyright: ignore[reportUnknownMemberType]
        payload,
        settings.jwt_secret.get_secret_value(),
        algorithm=_ALGORITHM,
        headers={"kid": settings.jwt_key_id},
    )


def _decode(settings: Settings, token: str, *, required: list[str]) -> dict[str, Any]:
    """Verify with the key named by the `kid` header (HS256 only). Raises TokenError."""
    try:
        header: dict[str, Any] = jwt.get_unverified_header(token)
        kid = header.get("kid")
        keys = _signing_keys(settings)
        if kid is None:
            # Tokens minted before key ids existed: verify with the active key only.
            key = settings.jwt_secret.get_secret_value()
        elif isinstance(kid, str) and kid in keys:
            key = keys[kid]
        else:
            raise TokenError("unknown key id")
        payload: dict[str, Any] = jwt.decode(  # pyright: ignore[reportUnknownMemberType]
            token,
            key,
            algorithms=[_ALGORITHM],
            issuer=settings.jwt_issuer,
            audience=settings.jwt_audience,
            options={"require": required, "verify_iat": True},
        )
        return payload
    except TokenError:
        raise
    except (jwt.PyJWTError, ValueError, TypeError, KeyError) as exc:
        raise TokenError("invalid token") from exc


def _base_claims(
    settings: Settings, *, subject: uuid.UUID, claim_type: str, issued: datetime, ttl: timedelta
) -> dict[str, Any]:
    return {
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "sub": str(subject),
        "type": claim_type,
        "jti": uuid.uuid4().hex,
        "iat": int(issued.timestamp()),
        "exp": int((issued + ttl).timestamp()),
    }


def create_access_token(
    settings: Settings,
    *,
    user_id: uuid.UUID,
    session_id: uuid.UUID,
    now: datetime | None = None,
    ttl: timedelta | None = None,
    claim_type: str = "access",
) -> str:
    payload = _base_claims(
        settings,
        subject=user_id,
        claim_type=claim_type,
        issued=now or datetime.now(UTC),
        ttl=ttl if ttl is not None else timedelta(seconds=settings.access_token_ttl_s),
    )
    payload["sid"] = str(session_id)
    return _encode(settings, payload)


def decode_access_token(settings: Settings, token: str) -> AccessClaims:
    """Validate signature (HS256 + known kid), exp/iat/iss/aud, required claims, type."""
    try:
        payload = _decode(settings, token, required=_ACCESS_CLAIMS)
        if payload["type"] != "access":
            raise TokenError("wrong token type")
        return AccessClaims(
            user_id=uuid.UUID(str(payload["sub"])),
            session_id=uuid.UUID(str(payload["sid"])),
            expires_at=datetime.fromtimestamp(int(payload["exp"]), UTC),
        )
    except TokenError:
        raise
    except (ValueError, TypeError, KeyError) as exc:
        raise TokenError("invalid token") from exc


def create_mfa_token(settings: Settings, *, user_id: uuid.UUID, now: datetime | None = None) -> str:
    """Short-lived proof that the password step succeeded; only usable at /auth/mfa/verify."""
    payload = _base_claims(
        settings,
        subject=user_id,
        claim_type="mfa",
        issued=now or datetime.now(UTC),
        ttl=timedelta(seconds=settings.mfa_token_ttl_s),
    )
    return _encode(settings, payload)


def decode_mfa_token(settings: Settings, token: str) -> uuid.UUID:
    try:
        payload = _decode(settings, token, required=_MFA_CLAIMS)
        if payload["type"] != "mfa":
            raise TokenError("wrong token type")
        return uuid.UUID(str(payload["sub"]))
    except TokenError:
        raise
    except (ValueError, TypeError, KeyError) as exc:
        raise TokenError("invalid token") from exc
