"""TOTP multi-factor authentication (real PostgreSQL and Redis)."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx2
import pyotp
import pytest
from sqlalchemy import text

from app.core.config import get_settings
from app.core.crypto import SecretBox
from app.core.security import create_mfa_token
from app.infra.container import Infrastructure

pytestmark = pytest.mark.integration

PASSWORD = "correct-horse-battery-staple"
API = "/api/v1/auth"
MFA = f"{API}/mfa"
EMAIL = "alice@example.com"


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def totp_code(secret: str, *, steps_ahead: int = 0) -> str:
    """A valid TOTP code for the current time step (+ N steps)."""
    step = int(time.time() // 30) + steps_ahead
    return pyotp.TOTP(secret).generate_otp(step)


async def login(client: httpx2.AsyncClient, password: str = PASSWORD) -> httpx2.Response:
    return await client.post(f"{API}/login", json={"email": EMAIL, "password": password})


async def enroll(client: httpx2.AsyncClient) -> dict[str, Any]:
    """Register, log in, enable MFA. Returns tokens, secret and recovery codes."""
    reg = await client.post(f"{API}/register", json={"email": EMAIL, "password": PASSWORD})
    assert reg.status_code == 201
    tokens = (await login(client)).json()
    h = bearer(tokens["access_token"])
    setup = await client.post(f"{MFA}/setup", headers=h)
    assert setup.status_code == 200
    secret = setup.json()["secret"]
    confirm = await client.post(f"{MFA}/confirm", headers=h, json={"code": totp_code(secret)})
    assert confirm.status_code == 200, confirm.text
    return {"tokens": tokens, "secret": secret, "recovery": confirm.json()["recovery_codes"]}


async def mfa_login(client: httpx2.AsyncClient) -> str:
    resp = await login(client)
    assert resp.status_code == 200
    body = resp.json()
    assert body["mfa_required"] is True
    return str(body["mfa_token"])


# ================================================================ enrollment


async def test_setup_returns_secret_and_otpauth_uri_and_stores_it_encrypted(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    await client.post(f"{API}/register", json={"email": EMAIL, "password": PASSWORD})
    tokens = (await login(client)).json()
    resp = await client.post(f"{MFA}/setup", headers=bearer(tokens["access_token"]))
    assert resp.status_code == 200
    body = resp.json()
    assert body["otpauth_uri"].startswith("otpauth://totp/")
    assert (
        "issuer=Agentic+RAG" in body["otpauth_uri"] or "issuer=Agentic%20RAG" in body["otpauth_uri"]
    )
    assert body["secret"] in body["otpauth_uri"]
    async with infra.session_factory() as session:
        stored = (await session.execute(text("SELECT secret_encrypted FROM user_mfa"))).scalar()
    assert stored != body["secret"]
    assert body["secret"] not in stored
    assert stored is not None
    assert SecretBox(get_settings()).decrypt(stored) == body["secret"]
    status = await client.get(f"{MFA}/status", headers=bearer(tokens["access_token"]))
    assert status.json() == {"enabled": False, "recovery_codes_remaining": 0}  # not confirmed yet


async def test_confirm_with_wrong_code_fails_and_right_code_enables(
    client: httpx2.AsyncClient,
) -> None:
    await client.post(f"{API}/register", json={"email": EMAIL, "password": PASSWORD})
    tokens = (await login(client)).json()
    h = bearer(tokens["access_token"])
    secret = (await client.post(f"{MFA}/setup", headers=h)).json()["secret"]

    wrong = await client.post(f"{MFA}/confirm", headers=h, json={"code": "000000"})
    assert wrong.status_code == 401
    assert wrong.json()["error"]["code"] == "INVALID_MFA_CODE"
    assert (await login(client)).json()["mfa_required"] is False  # still not enabled

    ok = await client.post(f"{MFA}/confirm", headers=h, json={"code": totp_code(secret)})
    assert ok.status_code == 200
    codes = ok.json()["recovery_codes"]
    assert len(codes) == get_settings().mfa_recovery_code_count
    assert len(set(codes)) == len(codes)
    status = await client.get(f"{MFA}/status", headers=h)
    assert status.json() == {"enabled": True, "recovery_codes_remaining": len(codes)}


async def test_confirm_without_setup_and_double_setup(client: httpx2.AsyncClient) -> None:
    await client.post(f"{API}/register", json={"email": EMAIL, "password": PASSWORD})
    tokens = (await login(client)).json()
    h = bearer(tokens["access_token"])
    none = await client.post(f"{MFA}/confirm", headers=h, json={"code": "123456"})
    assert none.status_code == 400
    assert none.json()["error"]["code"] == "MFA_NOT_PENDING"

    secret = (await client.post(f"{MFA}/setup", headers=h)).json()["secret"]
    again = (await client.post(f"{MFA}/setup", headers=h)).json()["secret"]  # restart pending setup
    assert again != secret
    await client.post(f"{MFA}/confirm", headers=h, json={"code": totp_code(again)})
    already = await client.post(f"{MFA}/setup", headers=h)
    assert already.status_code == 409
    assert already.json()["error"]["code"] == "MFA_ALREADY_ENABLED"


async def test_mfa_endpoints_require_authentication(client: httpx2.AsyncClient) -> None:
    for path in ("setup", "confirm", "disable", "status"):
        method = client.get if path == "status" else client.post
        kwargs: dict[str, Any] = {} if path in {"setup", "status"} else {"json": {}}
        assert (await method(f"{MFA}/{path}", **kwargs)).status_code in {401, 422}
    assert (await client.post(f"{MFA}/setup")).status_code == 401


# ================================================================ login with MFA


async def test_login_with_mfa_requires_second_step(client: httpx2.AsyncClient) -> None:
    info = await enroll(client)
    resp = await login(client)
    body = resp.json()
    assert resp.status_code == 200
    assert body["mfa_required"] is True
    assert body["access_token"] is None
    assert body["refresh_token"] is None
    assert body["mfa_token"]

    # the challenge token is not a session
    me = await client.get(f"{API}/me", headers=bearer(body["mfa_token"]))
    assert me.status_code == 401

    # a code one step ahead is accepted (clock-skew window) and is a later step than enrollment's
    done = await client.post(
        f"{MFA}/verify",
        json={"mfa_token": body["mfa_token"], "code": totp_code(info["secret"], steps_ahead=1)},
    )
    assert done.status_code == 200, done.text
    tokens = done.json()
    assert tokens["mfa_required"] is False
    assert (
        await client.get(f"{API}/me", headers=bearer(tokens["access_token"]))
    ).status_code == 200


async def test_totp_code_cannot_be_replayed(client: httpx2.AsyncClient) -> None:
    info = await enroll(client)
    code = totp_code(info["secret"], steps_ahead=1)
    first = await client.post(
        f"{MFA}/verify", json={"mfa_token": await mfa_login(client), "code": code}
    )
    assert first.status_code == 200
    replay = await client.post(
        f"{MFA}/verify", json={"mfa_token": await mfa_login(client), "code": code}
    )
    assert replay.status_code == 401
    assert replay.json()["error"]["code"] == "INVALID_MFA_CODE"
    # the confirm step already used the current step, so that code is a replay as well
    stale = await client.post(
        f"{MFA}/verify",
        json={"mfa_token": await mfa_login(client), "code": totp_code(info["secret"])},
    )
    assert stale.status_code == 401


async def test_codes_outside_the_time_window_and_malformed_codes_rejected(
    client: httpx2.AsyncClient,
) -> None:
    info = await enroll(client)
    token = await mfa_login(client)
    for code in (
        totp_code(info["secret"], steps_ahead=-3),
        totp_code(info["secret"], steps_ahead=3),
        "12345",
        "abcdef",
        "1234567",
    ):
        r = await client.post(f"{MFA}/verify", json={"mfa_token": token, "code": code})
        assert r.status_code in {401, 422}, code  # 422 = rejected by the schema (too short)
        assert r.status_code != 200


async def test_invalid_expired_and_wrong_type_mfa_tokens(client: httpx2.AsyncClient) -> None:
    info = await enroll(client)
    code = totp_code(info["secret"], steps_ahead=1)
    uid = __import__("uuid").UUID(
        __import__("jwt").decode(
            info["tokens"]["access_token"], options={"verify_signature": False}
        )["sub"]
    )
    expired = create_mfa_token(
        get_settings(), user_id=uid, now=datetime.now(UTC) - timedelta(hours=1)
    )
    for bad in ("x" * 40, expired, info["tokens"]["access_token"]):
        r = await client.post(f"{MFA}/verify", json={"mfa_token": bad, "code": code})
        assert r.status_code == 401
        assert r.json()["error"]["code"] == "INVALID_MFA_TOKEN"


async def test_recovery_codes_work_once_and_are_normalized(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    info = await enroll(client)
    recovery = info["recovery"][0]
    sloppy = recovery.replace("-", "").lower()  # no dash, lower case
    ok = await client.post(
        f"{MFA}/verify", json={"mfa_token": await mfa_login(client), "code": sloppy}
    )
    assert ok.status_code == 200
    reuse = await client.post(
        f"{MFA}/verify", json={"mfa_token": await mfa_login(client), "code": recovery}
    )
    assert reuse.status_code == 401
    status = await client.get(f"{MFA}/status", headers=bearer(ok.json()["access_token"]))
    assert status.json()["recovery_codes_remaining"] == len(info["recovery"]) - 1
    async with infra.session_factory() as session:
        hashes = (
            await session.execute(text("SELECT string_agg(code_hash, ',') FROM mfa_recovery_codes"))
        ).scalar()
    assert all(c not in hashes for c in info["recovery"])  # only hashes are stored


async def test_failed_attempts_lock_out_even_the_right_code(client: httpx2.AsyncClient) -> None:
    info = await enroll(client)
    token = await mfa_login(client)
    limit = get_settings().rl_mfa_limit
    for _ in range(limit):
        r = await client.post(f"{MFA}/verify", json={"mfa_token": token, "code": "000000"})
        assert r.status_code == 401
    locked = await client.post(
        f"{MFA}/verify", json={"mfa_token": token, "code": totp_code(info["secret"], steps_ahead=1)}
    )
    assert locked.status_code == 429
    assert "retry-after" in locked.headers


# ================================================================ disable


async def test_disable_requires_password_and_a_valid_code(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    info = await enroll(client)
    h = bearer(info["tokens"]["access_token"])
    bad_pw = await client.post(
        f"{MFA}/disable",
        headers=h,
        json={"password": "wrong-password-1", "code": totp_code(info["secret"], steps_ahead=1)},
    )
    assert bad_pw.status_code == 422
    bad_code = await client.post(
        f"{MFA}/disable", headers=h, json={"password": PASSWORD, "code": "000000"}
    )
    assert bad_code.status_code == 401
    assert (await client.get(f"{MFA}/status", headers=h)).json()["enabled"] is True

    ok = await client.post(
        f"{MFA}/disable",
        headers=h,
        json={"password": PASSWORD, "code": totp_code(info["secret"], steps_ahead=1)},
    )
    assert ok.status_code == 204
    assert (await client.get(f"{MFA}/status", headers=h)).json()["enabled"] is False
    async with infra.session_factory() as session:
        assert (await session.execute(text("SELECT count(*) FROM user_mfa"))).scalar() == 0
        assert (
            await session.execute(text("SELECT count(*) FROM mfa_recovery_codes"))
        ).scalar() == 0
    direct = await login(client)
    assert direct.json()["mfa_required"] is False
    assert direct.json()["access_token"]


async def test_disable_with_a_recovery_code(client: httpx2.AsyncClient) -> None:
    info = await enroll(client)
    resp = await client.post(
        f"{MFA}/disable",
        headers=bearer(info["tokens"]["access_token"]),
        json={"password": PASSWORD, "code": info["recovery"][3]},
    )
    assert resp.status_code == 204


async def test_mfa_state_is_per_user(client: httpx2.AsyncClient) -> None:
    await enroll(client)
    other = await client.post(
        f"{API}/register", json={"email": "bob@example.com", "password": PASSWORD}
    )
    assert other.status_code == 201
    bob = await client.post(f"{API}/login", json={"email": "bob@example.com", "password": PASSWORD})
    assert bob.json()["mfa_required"] is False  # Alice's MFA does not affect Bob
    status = await client.get(f"{MFA}/status", headers=bearer(bob.json()["access_token"]))
    assert status.json()["enabled"] is False


async def test_secrets_and_codes_never_appear_in_logs(
    client: httpx2.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    caplog.set_level(logging.DEBUG)
    info = await enroll(client)
    await client.post(
        f"{MFA}/verify",
        json={"mfa_token": await mfa_login(client), "code": info["recovery"][0]},
    )
    for secret in [info["secret"], *info["recovery"]]:
        assert secret not in caplog.text
