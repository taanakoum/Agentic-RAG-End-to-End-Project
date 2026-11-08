"""Auth API tests (gate G2) against real PostgreSQL and Redis."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx2
import pytest
from argon2 import PasswordHasher, Type
from sqlalchemy import text

from app.core.config import get_settings
from app.core.security import create_access_token
from app.infra.container import Infrastructure
from app.repositories.users import UserRepository
from tests.helpers import unverified_claims

pytestmark = pytest.mark.integration

PASSWORD = "correct-horse-battery-staple"
API = "/api/v1/auth"


async def register(
    client: httpx2.AsyncClient,
    email: str = "alice@example.com",
    password: str = PASSWORD,
    **extra: Any,
) -> httpx2.Response:
    return await client.post(
        f"{API}/register", json={"email": email, "password": password, **extra}
    )


async def login(
    client: httpx2.AsyncClient, email: str = "alice@example.com", password: str = PASSWORD
) -> httpx2.Response:
    return await client.post(f"{API}/login", json={"email": email, "password": password})


async def registered_tokens(
    client: httpx2.AsyncClient, email: str = "alice@example.com"
) -> dict[str, Any]:
    assert (await register(client, email)).status_code == 201
    resp = await login(client, email)
    assert resp.status_code == 200, resp.text
    return resp.json()


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def scalar(infra: Infrastructure, sql: str, **params: Any) -> Any:
    async with infra.session_factory() as session:
        return (await session.execute(text(sql), params)).scalar()


# ================================================================ registration


async def test_register_ok_and_no_secret_leak(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    resp = await register(client, "Alice@Example.com", display_name="  Alice  ")
    assert resp.status_code == 201
    body = resp.json()
    assert set(body) == {"id", "email", "display_name", "created_at"}
    assert body["email"] == "Alice@Example.com"
    assert body["display_name"] == "Alice"
    assert "password" not in resp.text.lower()
    assert "argon2" not in resp.text.lower()

    stored = await scalar(
        infra, "SELECT password_hash FROM users WHERE id = :i", i=uuid.UUID(body["id"])
    )
    assert stored.startswith("$argon2id$")
    assert PASSWORD not in stored
    normalized = await scalar(infra, "SELECT email_normalized FROM users")
    assert normalized == "alice@example.com"


@pytest.mark.parametrize(
    ("payload", "fields"),
    [
        ({}, {"email", "password"}),
        ({"email": "a@example.com"}, {"password"}),
        ({"password": PASSWORD}, {"email"}),
    ],
)
async def test_register_missing_fields(
    client: httpx2.AsyncClient, payload: dict[str, str], fields: set[str]
) -> None:
    resp = await client.post(f"{API}/register", json=payload)
    assert resp.status_code == 422
    err = resp.json()["error"]
    assert err["code"] == "VALIDATION_ERROR"
    assert {d["field"] for d in err["details"]} == fields
    assert err["request_id"] == resp.headers["x-request-id"]


async def test_register_rejects_unknown_fields(client: httpx2.AsyncClient) -> None:
    resp = await register(client, is_admin=True)
    assert resp.status_code == 422
    assert resp.json()["error"]["details"][0]["field"] == "is_admin"


@pytest.mark.parametrize("email", ["not-an-email", "a@", "@b.com", "a b@c.com", "a@@b.com"])
async def test_register_invalid_email(client: httpx2.AsyncClient, email: str) -> None:
    resp = await register(client, email)
    assert resp.status_code == 422
    details = resp.json()["error"]["details"]
    assert details[0]["field"] == "email"
    assert details[0]["code"] in {"invalid_email", "string_too_short"}  # schema bound or format


@pytest.mark.parametrize(
    ("password", "code"),
    [
        ("zq9-xk", "password_too_short"),
        ("            ", "password_blank"),
        ("aaaaaaaaaaaaaaaa", "password_too_repetitive"),
        ("password1234", "password_too_guessable"),
        ("alice-secret-123", "password_too_guessable"),  # contains the email local part
    ],
)
async def test_register_weak_password(client: httpx2.AsyncClient, password: str, code: str) -> None:
    resp = await register(client, password=password)
    assert resp.status_code == 422
    err = resp.json()["error"]
    assert code in {d["code"] for d in err["details"]}
    assert all(d["field"] == "password" for d in err["details"])
    assert password.strip() == "" or password not in resp.text  # the secret is never echoed


async def test_register_duplicate_email_variants(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    assert (await register(client, "bob@example.com")).status_code == 201
    for variant in (
        "bob@example.com",
        "BOB@EXAMPLE.COM",
        "  Bob@Example.com  ",
        "ＢＯＢ@example.com",
    ):
        resp = await register(client, variant)
        assert resp.status_code == 409, variant
        assert resp.json()["error"]["code"] == "EMAIL_ALREADY_REGISTERED"
    assert await scalar(infra, "SELECT count(*) FROM users") == 1


async def test_concurrent_duplicate_registration_exactly_one_wins(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    variants = ["carol@example.com", "Carol@Example.com", " CAROL@example.com", "carol@EXAMPLE.com"]
    responses = await asyncio.gather(*(register(client, variants[i % 4]) for i in range(24)))
    codes = sorted(r.status_code for r in responses)
    assert codes.count(201) == 1, codes
    assert codes.count(409) == 23, codes
    assert await scalar(infra, "SELECT count(*) FROM users") == 1


async def test_unique_constraint_is_final_authority(
    client: httpx2.AsyncClient, infra: Infrastructure, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even if the application-level pre-check misses (a race), PostgreSQL rejects the duplicate."""
    assert (await register(client, "dave@example.com")).status_code == 201

    async def no_precheck(self: UserRepository, email_normalized: str) -> None:
        return None

    monkeypatch.setattr(UserRepository, "get_by_email_normalized", no_precheck)
    resp = await register(client, "dave@example.com")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "EMAIL_ALREADY_REGISTERED"
    assert await scalar(infra, "SELECT count(*) FROM users") == 1


async def test_database_rejects_non_normalized_email(infra: Infrastructure) -> None:
    async with infra.session_factory() as session:
        with pytest.raises(Exception, match="email_normalized_lower"):
            await session.execute(
                text(
                    "INSERT INTO users (email, email_normalized, password_hash) "
                    "VALUES ('X@Y.com', 'X@Y.com', 'h')"
                )
            )


async def test_sql_injection_attempts(client: httpx2.AsyncClient, infra: Infrastructure) -> None:
    await register(client)
    inj = "' OR '1'='1"
    assert (await login(client, email=inj, password=inj)).status_code == 401
    assert (
        await login(client, email="alice@example.com' --", password=PASSWORD)
    ).status_code == 401
    resp = await register(client, "x@example.com'; DROP TABLE users;--")
    assert resp.status_code == 422
    assert await scalar(infra, "SELECT count(*) FROM users") == 1


# ================================================================ login


async def test_login_ok_and_me(client: httpx2.AsyncClient) -> None:
    tokens = await registered_tokens(client)
    assert tokens["token_type"] == "bearer"
    assert tokens["expires_in"] == get_settings().access_token_ttl_s
    claims = unverified_claims(tokens["access_token"])
    assert claims["type"] == "access"
    assert claims["iss"] == get_settings().jwt_issuer
    me = await client.get(f"{API}/me", headers=bearer(tokens["access_token"]))
    assert me.status_code == 200
    assert me.json()["email"] == "alice@example.com"
    assert "password_hash" not in me.text


async def test_login_wrong_password_and_unknown_account_are_indistinguishable(
    client: httpx2.AsyncClient,
) -> None:
    await register(client)
    wrong = await login(client, password="wrong-password-123")
    unknown = await login(client, email="nobody@example.com")
    assert wrong.status_code == unknown.status_code == 401
    w, u = wrong.json()["error"], unknown.json()["error"]
    assert (w["code"], w["message"], w["details"]) == (u["code"], u["message"], u["details"])
    assert w["code"] == "INVALID_CREDENTIALS"


async def test_login_normalizes_email(client: httpx2.AsyncClient) -> None:
    await register(client, "Erin@Example.com")
    assert (await login(client, email="  ERIN@example.COM ")).status_code == 200


async def test_login_rehashes_when_parameters_change(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    old = PasswordHasher(time_cost=1, memory_cost=1024, parallelism=1, type=Type.ID).hash(PASSWORD)
    async with infra.session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO users (email, email_normalized, password_hash) "
                "VALUES ('f@example.com', 'f@example.com', :h)"
            ),
            {"h": old},
        )
        await session.commit()
    assert (await login(client, "f@example.com")).status_code == 200
    new = await scalar(infra, "SELECT password_hash FROM users")
    assert new != old
    assert f"m={get_settings().argon2_memory_kib}" in new
    assert (await login(client, "f@example.com")).status_code == 200  # new hash verifies


async def test_inactive_user_cannot_login_or_use_token(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    tokens = await registered_tokens(client)
    async with infra.session_factory() as session:
        await session.execute(text("UPDATE users SET is_active = false"))
        await session.commit()
    assert (await login(client)).status_code == 401
    assert (
        await client.get(f"{API}/me", headers=bearer(tokens["access_token"]))
    ).status_code == 401
    refresh = await client.post(f"{API}/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert refresh.status_code == 401


# ================================================================ access token checks


async def test_me_requires_authentication(client: httpx2.AsyncClient) -> None:
    resp = await client.get(f"{API}/me")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"
    assert resp.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize(
    "header",
    ["Bearer garbage", "Bearer a.b.c", "Basic dXNlcjpwYXNz", "Bearer ", "bearer"],
)
async def test_me_rejects_bad_authorization_headers(
    client: httpx2.AsyncClient, header: str
) -> None:
    resp = await client.get(f"{API}/me", headers={"Authorization": header})
    assert resp.status_code == 401


async def test_expired_access_token(client: httpx2.AsyncClient) -> None:
    tokens = await registered_tokens(client)
    claims = unverified_claims(tokens["access_token"])
    expired = create_access_token(
        get_settings(),
        user_id=uuid.UUID(claims["sub"]),
        session_id=uuid.UUID(claims["sid"]),
        now=datetime.now(UTC) - timedelta(hours=2),
        ttl=timedelta(minutes=5),
    )
    resp = await client.get(f"{API}/me", headers=bearer(expired))
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "INVALID_TOKEN"


async def test_tokens_with_wrong_signature_issuer_audience_or_type(
    client: httpx2.AsyncClient,
) -> None:
    tokens = await registered_tokens(client)
    claims = unverified_claims(tokens["access_token"])
    uid, sid = uuid.UUID(claims["sub"]), uuid.UUID(claims["sid"])
    s = get_settings()
    forged = [
        create_access_token(
            s.model_copy(update={"jwt_secret": type(s.jwt_secret)("z" * 48)}),
            user_id=uid,
            session_id=sid,
        ),
        create_access_token(
            s.model_copy(update={"jwt_issuer": "evil"}), user_id=uid, session_id=sid
        ),
        create_access_token(
            s.model_copy(update={"jwt_audience": "evil"}), user_id=uid, session_id=sid
        ),
        create_access_token(s, user_id=uid, session_id=sid, claim_type="refresh"),
        tokens["refresh_token"],  # an opaque refresh token is not an access token
    ]
    for token in forged:
        resp = await client.get(f"{API}/me", headers=bearer(token))
        assert resp.status_code == 401, token[:20]
    # control: the genuine token still works
    assert (
        await client.get(f"{API}/me", headers=bearer(tokens["access_token"]))
    ).status_code == 200


async def test_token_for_deleted_user_or_unknown_session_rejected(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    tokens = await registered_tokens(client)
    claims = unverified_claims(tokens["access_token"])
    unknown_session = create_access_token(
        get_settings(), user_id=uuid.UUID(claims["sub"]), session_id=uuid.uuid4()
    )
    resp = await client.get(f"{API}/me", headers=bearer(unknown_session))
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "SESSION_REVOKED"
    async with infra.session_factory() as session:
        await session.execute(text("DELETE FROM users"))
        await session.commit()
    assert (
        await client.get(f"{API}/me", headers=bearer(tokens["access_token"]))
    ).status_code == 401


# ================================================================ refresh / logout


async def test_refresh_rotates_tokens(client: httpx2.AsyncClient, infra: Infrastructure) -> None:
    first = await registered_tokens(client)
    resp = await client.post(f"{API}/refresh", json={"refresh_token": first["refresh_token"]})
    assert resp.status_code == 200, resp.text
    second = resp.json()
    assert second["refresh_token"] != first["refresh_token"]
    assert (
        await client.get(f"{API}/me", headers=bearer(second["access_token"]))
    ).status_code == 200
    reasons = await scalar(
        infra,
        "SELECT string_agg(coalesce(revoked_reason, 'active'), ',' ORDER BY issued_at) "
        "FROM refresh_tokens",
    )
    assert reasons == "rotated,active"
    stored = await scalar(infra, "SELECT string_agg(token_hash, ',') FROM refresh_tokens")
    assert first["refresh_token"] not in stored
    assert second["refresh_token"] not in stored  # only hashes are stored


async def test_refresh_token_reuse_revokes_whole_family(client: httpx2.AsyncClient) -> None:
    first = await registered_tokens(client)
    second = (
        await client.post(f"{API}/refresh", json={"refresh_token": first["refresh_token"]})
    ).json()
    # The attacker (or buggy client) replays the already-rotated token...
    replay = await client.post(f"{API}/refresh", json={"refresh_token": first["refresh_token"]})
    assert replay.status_code == 401
    assert replay.json()["error"]["code"] == "INVALID_REFRESH_TOKEN"
    # ...so the legitimate newer token and its access token are dead too.
    newer = await client.post(f"{API}/refresh", json={"refresh_token": second["refresh_token"]})
    assert newer.status_code == 401
    me = await client.get(f"{API}/me", headers=bearer(second["access_token"]))
    assert me.status_code == 401
    assert me.json()["error"]["code"] == "SESSION_REVOKED"


async def test_concurrent_refresh_with_same_token_only_one_succeeds(
    client: httpx2.AsyncClient,
) -> None:
    tokens = await registered_tokens(client)
    responses = await asyncio.gather(
        *(
            client.post(f"{API}/refresh", json={"refresh_token": tokens["refresh_token"]})
            for _ in range(6)
        )
    )
    codes = sorted(r.status_code for r in responses)
    assert codes.count(200) == 1, codes
    assert codes.count(401) == 5, codes


async def test_expired_refresh_token(client: httpx2.AsyncClient, infra: Infrastructure) -> None:
    tokens = await registered_tokens(client)
    async with infra.session_factory() as session:
        await session.execute(
            text("UPDATE refresh_tokens SET expires_at = now() - interval '1 minute'")
        )
        await session.commit()
    resp = await client.post(f"{API}/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "INVALID_REFRESH_TOKEN"
    assert await scalar(infra, "SELECT revoked_reason FROM refresh_tokens") == "expired"
    # the access token's session has no live refresh token any more
    assert (
        await client.get(f"{API}/me", headers=bearer(tokens["access_token"]))
    ).status_code == 401


async def test_refresh_with_unknown_token(client: httpx2.AsyncClient) -> None:
    resp = await client.post(f"{API}/refresh", json={"refresh_token": "x" * 64})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "INVALID_REFRESH_TOKEN"


async def test_logout_invalidates_refresh_and_access_tokens(client: httpx2.AsyncClient) -> None:
    tokens = await registered_tokens(client)
    assert (
        await client.get(f"{API}/me", headers=bearer(tokens["access_token"]))
    ).status_code == 200
    out = await client.post(f"{API}/logout", json={"refresh_token": tokens["refresh_token"]})
    assert out.status_code == 204
    assert out.content == b""
    assert (
        await client.post(f"{API}/refresh", json={"refresh_token": tokens["refresh_token"]})
    ).status_code == 401
    me = await client.get(f"{API}/me", headers=bearer(tokens["access_token"]))
    assert me.status_code == 401
    assert me.json()["error"]["code"] == "SESSION_REVOKED"


async def test_logout_is_idempotent_and_other_sessions_survive(client: httpx2.AsyncClient) -> None:
    await register(client)
    a = (await login(client)).json()
    b = (await login(client)).json()  # second device
    assert (
        await client.post(f"{API}/logout", json={"refresh_token": a["refresh_token"]})
    ).status_code == 204
    assert (
        await client.post(f"{API}/logout", json={"refresh_token": a["refresh_token"]})
    ).status_code == 204
    assert (await client.post(f"{API}/logout", json={"refresh_token": "u" * 64})).status_code == 204
    assert (await client.get(f"{API}/me", headers=bearer(b["access_token"]))).status_code == 200


# ================================================================ rate limiting / brute force


async def test_login_per_ip_rate_limit(client_with_settings: Any) -> None:
    c = await client_with_settings(get_settings().model_copy(update={"rl_login_ip_limit": 3}))
    statuses = [
        (await login(c, "nobody@example.com", "whatever-123")).status_code for _ in range(5)
    ]
    assert statuses == [401, 401, 401, 429, 429]
    blocked = await login(c, "nobody@example.com", "whatever-123")
    assert blocked.json()["error"]["code"] == "RATE_LIMIT_EXCEEDED"
    assert int(blocked.headers["retry-after"]) >= 1


async def test_login_per_account_lockout_even_for_correct_password(
    client: httpx2.AsyncClient,
) -> None:
    await register(client)
    limit = get_settings().rl_login_account_limit
    for _ in range(limit):
        assert (await login(client, password="wrong-password-1")).status_code == 401
    locked = await login(client)  # correct password, but the account is locked out
    assert locked.status_code == 429
    assert "retry-after" in locked.headers


async def test_account_lockout_also_applies_to_unknown_accounts(client: httpx2.AsyncClient) -> None:
    """Same behaviour whether or not the account exists, so lockout cannot enumerate accounts."""
    limit = get_settings().rl_login_account_limit
    for _ in range(limit):
        assert (await login(client, "ghost@example.com", "wrong-password-1")).status_code == 401
    assert (await login(client, "ghost@example.com", "wrong-password-1")).status_code == 429


async def test_successful_login_resets_failure_counter(client: httpx2.AsyncClient) -> None:
    await register(client)
    limit = get_settings().rl_login_account_limit
    for _ in range(limit - 1):
        await login(client, password="wrong-password-1")
    assert (await login(client)).status_code == 200
    for _ in range(limit - 1):
        assert (await login(client, password="wrong-password-1")).status_code == 401
    assert (await login(client)).status_code == 200


async def test_register_rate_limit(client_with_settings: Any) -> None:
    c = await client_with_settings(get_settings().model_copy(update={"rl_register_ip_limit": 2}))
    statuses = [(await register(c, f"u{i}@example.com")).status_code for i in range(4)]
    assert statuses == [201, 201, 429, 429]


async def test_rate_limiter_fails_closed_when_redis_is_down(client_with_settings: Any) -> None:
    broken = get_settings().model_copy(update={"redis_url": "redis://127.0.0.1:1/0"})
    c = await client_with_settings(broken)
    resp = await login(c)
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "SERVICE_UNAVAILABLE"
    assert "127.0.0.1:1" not in resp.text


# ================================================================ HTTP hardening and error contract


async def test_body_too_large_rejected_by_content_length(client: httpx2.AsyncClient) -> None:
    big = "x" * (get_settings().max_body_bytes + 10)
    resp = await client.post(
        f"{API}/register", content=big, headers={"content-type": "application/json"}
    )
    assert resp.status_code == 413
    assert resp.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"
    assert resp.json()["error"]["request_id"] == resp.headers["x-request-id"]


async def test_body_too_large_rejected_while_streaming(client: httpx2.AsyncClient) -> None:
    limit = get_settings().max_body_bytes

    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(4):
            yield b"x" * (limit // 2)  # no Content-Length: chunked

    resp = await client.post(
        f"{API}/register", content=chunks(), headers={"content-type": "application/json"}
    )
    assert resp.status_code == 413


async def test_unknown_route_and_method_use_error_contract(client: httpx2.AsyncClient) -> None:
    nf = await client.get("/api/v1/nope")
    assert nf.status_code == 404
    assert nf.json()["error"]["code"] == "RESOURCE_NOT_FOUND"
    assert nf.json()["error"]["request_id"] == nf.headers["x-request-id"]
    bad = await client.get(f"{API}/login")
    assert bad.status_code == 405
    assert bad.json()["error"]["code"] == "METHOD_NOT_ALLOWED"


async def test_malformed_json_is_a_validation_error_not_a_crash(client: httpx2.AsyncClient) -> None:
    resp = await client.post(
        f"{API}/login",
        content=b'{"email": "a@b.com", "password": ',
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_unhandled_exception_returns_safe_500(infra: Infrastructure) -> None:
    from app.main import create_app

    app = create_app(get_settings())
    app.state.infra = infra

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("secret internal detail /etc/passwd")

    transport = httpx2.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.get("/boom")
    assert resp.status_code == 500
    body = resp.json()
    assert body["error"]["code"] == "INTERNAL_ERROR"
    assert "secret internal detail" not in resp.text
    assert "Traceback" not in resp.text
    assert body["error"]["request_id"] == resp.headers["x-request-id"]


async def test_openapi_documents_error_contract(client: httpx2.AsyncClient) -> None:
    spec = (await client.get("/openapi.json")).json()
    login_responses = spec["paths"]["/api/v1/auth/login"]["post"]["responses"]
    assert {"200", "401", "422", "429", "503"} <= set(login_responses)
    assert "ErrorResponse" in json.dumps(login_responses["401"])


# ================================================================ logging hygiene


async def test_secrets_never_appear_in_logs(
    client: httpx2.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    await register(client)
    tokens = (await login(client)).json()
    await login(client, password="wrong-password-xyz")
    await client.post(f"{API}/refresh", json={"refresh_token": tokens["refresh_token"]})
    await client.post(f"{API}/refresh", json={"refresh_token": tokens["refresh_token"]})  # reuse
    await client.get(f"{API}/me", headers=bearer(tokens["access_token"]))
    text_logs = caplog.text
    for secret in (PASSWORD, "wrong-password-xyz", tokens["access_token"], tokens["refresh_token"]):
        assert secret not in text_logs
