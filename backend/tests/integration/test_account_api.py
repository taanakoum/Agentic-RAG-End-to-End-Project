"""Password change/reset and email verification (real PostgreSQL, Redis, file email backend)."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx2
import pytest
from sqlalchemy import text

from app.core.config import get_settings
from app.infra.container import Infrastructure
from tests.helpers import read_outbox, token_from

pytestmark = pytest.mark.integration

PASSWORD = "correct-horse-battery-staple"
NEW_PASSWORD = "another-long-passphrase-2026"
API = "/api/v1/auth"


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def outbox() -> list[Any]:
    return read_outbox(get_settings().email_file_dir)


async def make_user(client: httpx2.AsyncClient, email: str = "alice@example.com") -> dict[str, Any]:
    assert (
        await client.post(f"{API}/register", json={"email": email, "password": PASSWORD})
    ).status_code == 201
    resp = await client.post(f"{API}/login", json={"email": email, "password": PASSWORD})
    assert resp.status_code == 200
    return resp.json()


async def login(
    client: httpx2.AsyncClient, email: str = "alice@example.com", password: str = PASSWORD
) -> httpx2.Response:
    return await client.post(f"{API}/login", json={"email": email, "password": password})


async def forgot(client: httpx2.AsyncClient, email: str) -> httpx2.Response:
    return await client.post(f"{API}/password/forgot", json={"email": email})


async def scalar(infra: Infrastructure, sql: str, **params: Any) -> Any:
    async with infra.session_factory() as session:
        return (await session.execute(text(sql), params)).scalar()


# ================================================================ email verification


async def test_registration_sends_verification_email_and_verifying_works(
    client: httpx2.AsyncClient,
) -> None:
    tokens = await make_user(client)
    (mail,) = outbox()
    assert mail["To"] == "alice@example.com"
    assert "Verify" in mail["Subject"]
    me = await client.get(f"{API}/me", headers=bearer(tokens["access_token"]))
    assert me.json()["email_verified"] is False

    token = token_from(mail, "Verification token")
    ok = await client.post(f"{API}/email/verify", json={"token": token})
    assert ok.status_code == 204
    me = await client.get(f"{API}/me", headers=bearer(tokens["access_token"]))
    assert me.json()["email_verified"] is True


async def test_verification_token_is_single_use_and_stored_hashed(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    await make_user(client)
    token = token_from(outbox()[0], "Verification token")
    assert (await client.post(f"{API}/email/verify", json={"token": token})).status_code == 204
    again = await client.post(f"{API}/email/verify", json={"token": token})
    assert again.status_code == 400
    assert again.json()["error"]["code"] == "INVALID_VERIFICATION_TOKEN"
    stored = await scalar(infra, "SELECT string_agg(token_hash, ',') FROM one_time_tokens")
    assert token not in stored


async def test_expired_and_garbage_verification_tokens_rejected(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    await make_user(client)
    token = token_from(outbox()[0], "Verification token")
    async with infra.session_factory() as session:
        await session.execute(
            text("UPDATE one_time_tokens SET expires_at = now() - interval '1 s'")
        )
        await session.commit()
    for bad in (token, "x" * 43):
        resp = await client.post(f"{API}/email/verify", json={"token": bad})
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "INVALID_VERIFICATION_TOKEN"


async def test_verification_request_invalidates_older_token(client: httpx2.AsyncClient) -> None:
    tokens = await make_user(client)
    first = token_from(outbox()[0], "Verification token")
    resp = await client.post(f"{API}/email/verify/request", headers=bearer(tokens["access_token"]))
    assert resp.status_code == 202
    mails = outbox()
    assert len(mails) == 2
    second = token_from(mails[1], "Verification token")
    assert first != second
    assert (await client.post(f"{API}/email/verify", json={"token": first})).status_code == 400
    assert (await client.post(f"{API}/email/verify", json={"token": second})).status_code == 204


async def test_no_verification_email_for_already_verified_user(client: httpx2.AsyncClient) -> None:
    tokens = await make_user(client)
    token = token_from(outbox()[0], "Verification token")
    await client.post(f"{API}/email/verify", json={"token": token})
    await client.post(f"{API}/email/verify/request", headers=bearer(tokens["access_token"]))
    assert len(outbox()) == 1


async def test_verification_request_requires_auth_and_is_rate_limited(
    client: httpx2.AsyncClient,
) -> None:
    assert (await client.post(f"{API}/email/verify/request")).status_code == 401
    tokens = await make_user(client)
    limit = get_settings().rl_forgot_account_limit
    codes = [
        (
            await client.post(f"{API}/email/verify/request", headers=bearer(tokens["access_token"]))
        ).status_code
        for _ in range(limit + 1)
    ]
    assert codes == [202] * limit + [429]


# ================================================================ change password


async def test_change_password_signs_out_other_sessions_but_not_current(
    client: httpx2.AsyncClient,
) -> None:
    first = await make_user(client)
    second = (await login(client)).json()  # another device

    resp = await client.post(
        f"{API}/password/change",
        headers=bearer(first["access_token"]),
        json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
    )
    assert resp.status_code == 204
    assert (await client.get(f"{API}/me", headers=bearer(first["access_token"]))).status_code == 200
    gone = await client.get(f"{API}/me", headers=bearer(second["access_token"]))
    assert gone.status_code == 401
    assert (
        await client.post(f"{API}/refresh", json={"refresh_token": second["refresh_token"]})
    ).status_code == 401
    assert (await login(client)).status_code == 401  # old password
    assert (await login(client, password=NEW_PASSWORD)).status_code == 200
    assert any("password was changed" in m["Subject"] for m in outbox())


async def test_change_password_validation(client: httpx2.AsyncClient) -> None:
    tokens = await make_user(client)
    h = bearer(tokens["access_token"])

    wrong = await client.post(
        f"{API}/password/change",
        headers=h,
        json={"current_password": "not-my-password-1", "new_password": NEW_PASSWORD},
    )
    assert wrong.status_code == 422
    assert wrong.json()["error"]["details"][0]["field"] == "current_password"
    assert "not-my-password-1" not in wrong.text

    weak = await client.post(
        f"{API}/password/change",
        headers=h,
        json={"current_password": PASSWORD, "new_password": "short"},
    )
    assert weak.status_code == 422
    assert weak.json()["error"]["details"][0]["field"] == "new_password"

    same = await client.post(
        f"{API}/password/change",
        headers=h,
        json={"current_password": PASSWORD, "new_password": PASSWORD},
    )
    assert same.status_code == 422

    anon = await client.post(
        f"{API}/password/change",
        json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
    )
    assert anon.status_code == 401
    assert (await login(client)).status_code == 200  # password unchanged by all failures


async def test_change_password_brute_force_is_limited(client: httpx2.AsyncClient) -> None:
    tokens = await make_user(client)
    h = bearer(tokens["access_token"])
    limit = get_settings().rl_pwchange_limit
    for _ in range(limit):
        r = await client.post(
            f"{API}/password/change",
            headers=h,
            json={"current_password": "wrong-password-1", "new_password": NEW_PASSWORD},
        )
        assert r.status_code == 422
    locked = await client.post(
        f"{API}/password/change",
        headers=h,
        json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
    )
    assert locked.status_code == 429


# ================================================================ forgot / reset password


async def test_forgot_password_never_reveals_whether_the_account_exists(
    client: httpx2.AsyncClient,
) -> None:
    await make_user(client)
    known = await forgot(client, "alice@example.com")
    unknown = await forgot(client, "nobody@example.com")
    assert known.status_code == unknown.status_code == 202
    assert known.json() == unknown.json()
    reset_mails = [m for m in outbox() if "Reset" in m["Subject"]]
    assert len(reset_mails) == 1
    assert reset_mails[0]["To"] == "alice@example.com"


async def test_full_password_reset_flow(client: httpx2.AsyncClient) -> None:
    tokens = await make_user(client)
    await forgot(client, "Alice@Example.com")
    mail = next(m for m in outbox() if "Reset" in m["Subject"])
    token = token_from(mail, "Reset token")

    resp = await client.post(
        f"{API}/password/reset", json={"token": token, "new_password": NEW_PASSWORD}
    )
    assert resp.status_code == 204
    assert (await login(client)).status_code == 401
    assert (await login(client, password=NEW_PASSWORD)).status_code == 200
    # every pre-existing session is gone
    assert (
        await client.get(f"{API}/me", headers=bearer(tokens["access_token"]))
    ).status_code == 401
    assert (
        await client.post(f"{API}/refresh", json={"refresh_token": tokens["refresh_token"]})
    ).status_code == 401
    # single use
    again = await client.post(
        f"{API}/password/reset", json={"token": token, "new_password": "yet-another-passphrase-9"}
    )
    assert again.status_code == 400
    assert again.json()["error"]["code"] == "INVALID_RESET_TOKEN"
    assert any("password was reset" in m["Subject"] for m in outbox())


async def test_weak_new_password_does_not_consume_the_reset_token(
    client: httpx2.AsyncClient,
) -> None:
    await make_user(client)
    await forgot(client, "alice@example.com")
    token = token_from(next(m for m in outbox() if "Reset" in m["Subject"]), "Reset token")
    weak = await client.post(
        f"{API}/password/reset", json={"token": token, "new_password": "short"}
    )
    assert weak.status_code == 422
    assert weak.json()["error"]["details"][0]["field"] == "new_password"
    ok = await client.post(
        f"{API}/password/reset", json={"token": token, "new_password": NEW_PASSWORD}
    )
    assert ok.status_code == 204


async def test_concurrent_use_of_one_reset_token_succeeds_exactly_once(
    client: httpx2.AsyncClient,
) -> None:
    await make_user(client)
    await forgot(client, "alice@example.com")
    token = token_from(next(m for m in outbox() if "Reset" in m["Subject"]), "Reset token")
    responses = await asyncio.gather(
        *(
            client.post(
                f"{API}/password/reset",
                json={"token": token, "new_password": f"concurrent-passphrase-{i}-x"},
            )
            for i in range(8)
        )
    )
    codes = sorted(r.status_code for r in responses)
    assert codes.count(204) == 1, codes
    assert codes.count(400) == 7, codes


async def test_expired_reset_token_rejected(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    await make_user(client)
    await forgot(client, "alice@example.com")
    token = token_from(next(m for m in outbox() if "Reset" in m["Subject"]), "Reset token")
    async with infra.session_factory() as session:
        await session.execute(
            text(
                "UPDATE one_time_tokens SET expires_at = now() - interval '1 s' "
                "WHERE purpose = 'password_reset'"
            )
        )
        await session.commit()
    resp = await client.post(
        f"{API}/password/reset", json={"token": token, "new_password": NEW_PASSWORD}
    )
    assert resp.status_code == 400
    assert (await login(client)).status_code == 200  # password untouched


async def test_new_reset_request_invalidates_the_previous_token(client: httpx2.AsyncClient) -> None:
    await make_user(client)
    await forgot(client, "alice@example.com")
    await forgot(client, "alice@example.com")
    first, second = (token_from(m, "Reset token") for m in outbox() if "Reset" in m["Subject"])
    assert (
        await client.post(
            f"{API}/password/reset", json={"token": first, "new_password": NEW_PASSWORD}
        )
    ).status_code == 400
    assert (
        await client.post(
            f"{API}/password/reset", json={"token": second, "new_password": NEW_PASSWORD}
        )
    ).status_code == 204


async def test_forgot_password_is_rate_limited_per_account_identically_for_unknown(
    client: httpx2.AsyncClient,
) -> None:
    await make_user(client)
    limit = get_settings().rl_forgot_account_limit
    for email in ("alice@example.com", "ghost@example.com"):
        codes = [(await forgot(client, email)).status_code for _ in range(limit + 1)]
        assert codes == [202] * limit + [429], email


async def test_inactive_account_gets_no_reset_email_and_reset_token_is_dead(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    await make_user(client)
    await forgot(client, "alice@example.com")
    token = token_from(next(m for m in outbox() if "Reset" in m["Subject"]), "Reset token")
    async with infra.session_factory() as session:
        await session.execute(text("UPDATE users SET is_active = false"))
        await session.commit()
    before = len(outbox())
    await forgot(client, "alice@example.com")
    assert len(outbox()) == before
    dead = await client.post(
        f"{API}/password/reset", json={"token": token, "new_password": NEW_PASSWORD}
    )
    assert dead.status_code == 400


async def test_email_failure_does_not_break_registration_or_leak(
    client_with_settings: Any,
) -> None:
    """If SMTP is down the API still answers normally; the failure is only logged."""
    broken = get_settings().model_copy(
        update={"email_backend": "smtp", "smtp_host": "127.0.0.1", "smtp_port": 1}
    )
    c = await client_with_settings(broken)
    resp = await c.post(f"{API}/register", json={"email": "z@example.com", "password": PASSWORD})
    assert resp.status_code == 201
    forgot_resp = await forgot(c, "z@example.com")
    assert forgot_resp.status_code == 202


async def test_tokens_never_appear_in_logs(
    client: httpx2.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    caplog.set_level(logging.DEBUG)
    await make_user(client)
    await forgot(client, "alice@example.com")
    mails = outbox()
    tokens = [token_from(m, "Verification token") for m in mails if "Verify" in m["Subject"]]
    tokens += [token_from(m, "Reset token") for m in mails if "Reset" in m["Subject"]]
    assert len(tokens) == 2
    for t in tokens:
        assert t not in caplog.text
