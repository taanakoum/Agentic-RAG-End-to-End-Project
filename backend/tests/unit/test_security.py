from __future__ import annotations

import base64
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from argon2 import PasswordHasher, Type

from app.core.config import Settings
from app.core.exceptions import ResourceNotFoundError
from app.core.ownership import require_owner
from app.core.security import (
    InvalidEmailError,
    PasswordService,
    TokenError,
    create_access_token,
    decode_access_token,
    generate_opaque_token,
    hash_opaque_token,
    normalize_email,
    password_policy_violations,
)
from app.infra.rate_limit import subject_key
from tests.helpers import forge_jwt


@pytest.fixture(scope="module")
def settings() -> Settings:
    return Settings.build(
        app_env="dev",
        jwt_secret="s" * 48,
        mfa_encryption_key="dGVzdC1vbmx5LWZlcm5ldC1rZXktMzItYnl0ZXMhISE=",
        argon2_memory_kib=8192,
        argon2_time_cost=1,
        argon2_parallelism=1,
    )


# ---------------------------------------------------------------- email


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Alice@Example.COM", "alice@example.com"),
        ("  alice@example.com  ", "alice@example.com"),
        ("ＡＬＩＣＥ@example.com", "alice@example.com"),  # fullwidth letters -> NFKC
        ("user+tag@Example.org", "user+tag@example.org"),
    ],
)
def test_normalize_email(raw: str, expected: str) -> None:
    assert normalize_email(raw) == expected


@pytest.mark.parametrize(
    "raw", ["", "plain", "a@", "@b.com", "a b@c.com", "a@b", "a@@b.com", "x" * 400 + "@b.com"]
)
def test_normalize_email_rejects_invalid(raw: str) -> None:
    with pytest.raises(InvalidEmailError):
        normalize_email(raw)


def test_variants_normalize_identically() -> None:
    variants = ["Bob@Example.com", "bob@example.COM", " BOB@example.com", "ＢＯＢ@example.com"]
    assert len({normalize_email(v) for v in variants}) == 1


# ---------------------------------------------------------------- password policy


def test_policy_accepts_strong_password(settings: Settings) -> None:
    assert (
        password_policy_violations("correct-horse-battery", email="a@b.com", settings=settings)
        == []
    )


@pytest.mark.parametrize(
    ("password", "email", "code"),
    [
        ("short1!", "a@b.com", "password_too_short"),
        ("x" * 200, "a@b.com", "password_too_long"),
        ("            ", "a@b.com", "password_blank"),
        ("aaaaaaaaaaaaaaaa", "a@b.com", "password_too_repetitive"),
        ("password1234", "a@b.com", "password_too_guessable"),
        ("my-alice-password-1", "alice@example.com", "password_too_guessable"),
    ],
)
def test_policy_violations(password: str, email: str, code: str, settings: Settings) -> None:
    assert code in password_policy_violations(password, email=email, settings=settings)


# ---------------------------------------------------------------- Argon2id


async def test_hash_is_argon2id_and_verifies(settings: Settings) -> None:
    svc = PasswordService(settings)
    hashed = await svc.hash("correct-horse-battery")
    assert hashed.startswith("$argon2id$")
    assert "correct-horse-battery" not in hashed
    assert await svc.verify(hashed, "correct-horse-battery") is True
    assert await svc.verify(hashed, "wrong-password-here") is False
    assert await svc.hash("correct-horse-battery") != hashed  # random salt


async def test_verify_garbage_hash_is_false_not_error(settings: Settings) -> None:
    svc = PasswordService(settings)
    assert await svc.verify("not-a-hash", "whatever") is False


def test_needs_rehash_when_parameters_change(settings: Settings) -> None:
    svc = PasswordService(settings)
    weaker = PasswordHasher(time_cost=1, memory_cost=1024, parallelism=1, type=Type.ID).hash("pw")
    assert svc.needs_rehash(weaker) is True
    assert svc.needs_rehash(svc.hash_sync("pw")) is False


async def test_concurrent_hashing_is_bounded(settings: Settings) -> None:
    import asyncio

    svc = PasswordService(settings.model_copy(update={"max_concurrent_hashes": 2}))
    results = await asyncio.gather(*(svc.hash(f"password-number-{i}") for i in range(6)))
    assert len(set(results)) == 6


# ---------------------------------------------------------------- opaque tokens


def test_opaque_tokens_are_random_and_hashed() -> None:
    a, b = generate_opaque_token(), generate_opaque_token()
    assert a != b
    assert len(a) >= 60
    digest = hash_opaque_token(a)
    assert len(digest) == 64
    assert a not in digest
    assert digest == hash_opaque_token(a) != hash_opaque_token(b)


# ---------------------------------------------------------------- JWT


def _b64(data: dict[str, Any]) -> str:
    raw = json.dumps(data, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def test_access_token_roundtrip(settings: Settings) -> None:
    uid, sid = uuid.uuid4(), uuid.uuid4()
    claims = decode_access_token(
        settings, create_access_token(settings, user_id=uid, session_id=sid)
    )
    assert claims.user_id == uid
    assert claims.session_id == sid
    assert claims.expires_at > datetime.now(UTC)


def test_expired_token_rejected(settings: Settings) -> None:
    token = create_access_token(
        settings,
        user_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        now=datetime.now(UTC) - timedelta(hours=1),
        ttl=timedelta(seconds=60),
    )
    with pytest.raises(TokenError):
        decode_access_token(settings, token)


def test_token_issued_in_the_future_rejected(settings: Settings) -> None:
    token = create_access_token(
        settings,
        user_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        now=datetime.now(UTC) + timedelta(hours=1),
    )
    with pytest.raises(TokenError):
        decode_access_token(settings, token)


def test_wrong_signature_rejected(settings: Settings) -> None:
    other = settings.model_copy(update={"jwt_secret": settings.jwt_secret.__class__("o" * 48)})
    token = create_access_token(other, user_id=uuid.uuid4(), session_id=uuid.uuid4())
    with pytest.raises(TokenError):
        decode_access_token(settings, token)


@pytest.mark.parametrize(
    "overrides",
    [{"jwt_issuer": "someone-else"}, {"jwt_audience": "other-audience"}],
)
def test_wrong_issuer_or_audience_rejected(settings: Settings, overrides: dict[str, str]) -> None:
    other = settings.model_copy(update=overrides)
    token = create_access_token(other, user_id=uuid.uuid4(), session_id=uuid.uuid4())
    with pytest.raises(TokenError):
        decode_access_token(settings, token)


def test_wrong_token_type_rejected(settings: Settings) -> None:
    token = create_access_token(
        settings, user_id=uuid.uuid4(), session_id=uuid.uuid4(), claim_type="refresh"
    )
    with pytest.raises(TokenError):
        decode_access_token(settings, token)


def test_alg_none_rejected(settings: Settings) -> None:
    now = int(datetime.now(UTC).timestamp())
    payload = {
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "sub": str(uuid.uuid4()),
        "sid": str(uuid.uuid4()),
        "type": "access",
        "jti": "x",
        "iat": now,
        "exp": now + 600,
    }
    token = f"{_b64({'alg': 'none', 'typ': 'JWT'})}.{_b64(payload)}."
    with pytest.raises(TokenError):
        decode_access_token(settings, token)


@pytest.mark.filterwarnings("ignore:The HMAC key is")
def test_other_hmac_algorithm_rejected(settings: Settings) -> None:
    now = int(datetime.now(UTC).timestamp())
    payload = {
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "sub": str(uuid.uuid4()),
        "sid": str(uuid.uuid4()),
        "type": "access",
        "jti": "x",
        "iat": now,
        "exp": now + 600,
    }
    token = forge_jwt(payload, settings.jwt_secret.get_secret_value(), "HS512")
    with pytest.raises(TokenError):
        decode_access_token(settings, token)


@pytest.mark.parametrize("claim", ["exp", "iat", "iss", "aud", "sub", "type", "sid", "jti"])
def test_missing_required_claim_rejected(settings: Settings, claim: str) -> None:
    now = int(datetime.now(UTC).timestamp())
    payload: dict[str, Any] = {
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "sub": str(uuid.uuid4()),
        "sid": str(uuid.uuid4()),
        "type": "access",
        "jti": "x",
        "iat": now,
        "exp": now + 600,
    }
    del payload[claim]
    token = forge_jwt(payload, settings.jwt_secret.get_secret_value(), "HS256")
    with pytest.raises(TokenError):
        decode_access_token(settings, token)


@pytest.mark.parametrize("garbage", ["", "abc", "a.b.c", "....", "Bearer x"])
def test_garbage_tokens_rejected(settings: Settings, garbage: str) -> None:
    with pytest.raises(TokenError):
        decode_access_token(settings, garbage)


# ---------------------------------------------------------------- ownership + rate-limit keys


def test_require_owner() -> None:
    me, other = uuid.uuid4(), uuid.uuid4()
    require_owner(me, me)
    with pytest.raises(ResourceNotFoundError) as other_exc:
        require_owner(other, me)
    with pytest.raises(ResourceNotFoundError) as missing_exc:
        require_owner(None, me)
    # Another user's resource is indistinguishable from a missing one.
    assert other_exc.value.status_code == missing_exc.value.status_code == 404
    assert other_exc.value.message == missing_exc.value.message


def test_rate_limit_subject_key_has_no_pii() -> None:
    key = subject_key("alice@example.com")
    assert "alice" not in key
    assert key == subject_key("alice@example.com") != subject_key("bob@example.com")
