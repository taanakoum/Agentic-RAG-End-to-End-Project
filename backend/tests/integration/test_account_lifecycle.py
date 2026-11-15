"""Breached passwords, verified-email gating and permanent account deletion."""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

import httpx2
import pyotp
import pytest
from sqlalchemy import text

from app.core.breached import is_breached_password
from app.core.config import get_settings
from app.infra.container import Infrastructure
from app.ingestion.outbox import dispatch_outbox
from app.ingestion.reconcile import reconcile
from app.ingestion.runtime import WorkerRuntime
from app.tasks.celery_app import celery_app
from tests.helpers import read_outbox, token_from
from tests.integration.docs_helpers import (
    API,
    PASSWORD,
    new_user,
    points_for,
    scalar,
    stored_object_count,
    upload,
    wait_for_status,
)
from tests.pdfs import sample_text_pdf

pytestmark = pytest.mark.integration

AUTH = f"{API}/auth"


# ================================================================ breached passwords


@pytest.mark.parametrize(
    "password",
    [
        "Password123456",  # a known password plus digits
        "qwertyuiop1234",
        "Welcome2026!!",  # known word decorated with digits and symbols
        "letmein12345",
        "P@ssw0rd!2026",  # leet-speak variant
        "dragon2026!!!!",
    ],
)
def test_common_and_decorated_passwords_are_detected(password: str) -> None:
    assert is_breached_password(password) is True


@pytest.mark.parametrize(
    "password",
    ["correct-horse-battery-staple", "another-long-passphrase-2026", "x7Qm-vLp2-Zt9R-wK4n"],
)
def test_unusual_passphrases_are_accepted(password: str) -> None:
    assert is_breached_password(password) is False


async def test_registration_rejects_breached_passwords(client: httpx2.AsyncClient) -> None:
    resp = await client.post(
        f"{AUTH}/register", json={"email": "a@example.com", "password": "Welcome2026!!"}
    )
    assert resp.status_code == 422
    codes = {d["code"] for d in resp.json()["error"]["details"]}
    assert "password_breached" in codes
    assert "Welcome2026" not in resp.text


async def test_password_change_and_reset_also_reject_breached_passwords(
    client: httpx2.AsyncClient,
) -> None:
    headers = await new_user(client)
    change = await client.post(
        f"{AUTH}/password/change",
        headers=headers,
        json={"current_password": PASSWORD, "new_password": "Welcome2026!!"},
    )
    assert change.status_code == 422
    assert "password_breached" in {d["code"] for d in change.json()["error"]["details"]}
    await client.post(f"{AUTH}/password/forgot", json={"email": "alice@example.com"})
    mail = next(m for m in read_outbox(get_settings().email_file_dir) if "Reset" in m["Subject"])
    reset = await client.post(
        f"{AUTH}/password/reset",
        json={"token": token_from(mail, "Reset token"), "new_password": "letmein12345"},
    )
    assert reset.status_code == 422


# ================================================================ verified-email gating


async def test_gating_blocks_unverified_users_until_they_verify(
    client_with_settings: Any,
) -> None:
    gated = await client_with_settings(
        get_settings().model_copy(update={"require_verified_email": True})
    )
    headers = await new_user(gated)

    blocked = await upload(gated, headers, sample_text_pdf("gate"))
    assert blocked.status_code == 403
    assert blocked.json()["error"]["code"] == "EMAIL_NOT_VERIFIED"
    # reading is not gated
    assert (await gated.get(f"{API}/documents", headers=headers)).status_code == 200
    assert (await gated.get(f"{AUTH}/me", headers=headers)).status_code == 200

    mail = next(m for m in read_outbox(get_settings().email_file_dir) if "Verify" in m["Subject"])
    verify = await gated.post(
        f"{AUTH}/email/verify", json={"token": token_from(mail, "Verification token")}
    )
    assert verify.status_code == 204
    allowed = await upload(gated, headers, sample_text_pdf("gate"))
    assert allowed.status_code == 202


async def test_gating_covers_reindex_and_retry_and_is_off_by_default(
    client: httpx2.AsyncClient, client_with_settings: Any
) -> None:
    headers = await new_user(client)  # default settings: no gating, upload works
    up = await upload(client, headers, sample_text_pdf("default"))
    assert up.status_code == 202
    gated = await client_with_settings(
        get_settings().model_copy(update={"require_verified_email": True})
    )
    document_id, job_id = up.json()["document"]["id"], up.json()["job_id"]
    for method, path in (
        ("POST", f"documents/{document_id}/reindex"),
        ("POST", f"ingestion/{job_id}/retry"),
    ):
        resp = await gated.request(method, f"{API}/{path}", headers=headers)
        assert resp.status_code == 403, path
        assert resp.json()["error"]["code"] == "EMAIL_NOT_VERIFIED"


def test_prod_requires_verified_email_gating() -> None:
    from app.core.config import Settings

    base: dict[str, Any] = {
        "app_env": "prod",
        "postgres_sslmode": "verify-full",
        "postgres_password": "a-long-unique-secret",
        "log_json": True,
        "argon2_memory_kib": 65536,
        "argon2_time_cost": 3,
        "email_backend": "smtp",
        "smtp_host": "smtp.example.com",
        "jwt_secret": "x" * 40,
        "mfa_encryption_key": "dGVzdC1vbmx5LWZlcm5ldC1rZXktMzItYnl0ZXMhISE=",
    }
    with pytest.raises(ValueError, match="require_verified_email"):
        Settings.build(**base)
    assert Settings.build(**base, require_verified_email=True).require_verified_email is True


# ================================================================ account deletion (real worker)


def totp_now(secret: str, steps_ahead: int = 0) -> str:
    return pyotp.TOTP(secret).generate_otp(int(time.time() // 30) + steps_ahead)


def dispatch_now(infra: Infrastructure) -> int:
    return dispatch_outbox(infra.sync_session_factory, celery_app, batch_size=100)


async def wait_until(predicate: Any, *, tries: int = 150) -> None:
    for _ in range(tries):
        if await predicate():
            return
        await asyncio.sleep(0.2)
    raise AssertionError("condition not reached in time")


async def test_account_deletion_removes_everything_and_frees_the_email(
    client: httpx2.AsyncClient, infra: Infrastructure, runtime: WorkerRuntime
) -> None:
    alice = await new_user(client, "alice@example.com")
    bob = await new_user(client, "bob@example.com")
    a_doc = (await upload(client, alice, sample_text_pdf("alice-doc"))).json()["document"]["id"]
    bob_doc = (await upload(client, bob, sample_text_pdf("bob-doc"))).json()["document"]["id"]
    await wait_for_status(client, alice, a_doc, {"completed"})
    await wait_for_status(client, bob, bob_doc, {"completed"})
    alice_id = await scalar(
        infra, "SELECT id::text FROM users WHERE email_normalized = 'alice@example.com'"
    )
    assert points_for(a_doc) and stored_object_count() == 2

    wrong = await client.request(
        "DELETE", f"{AUTH}/account", headers=alice, json={"password": "not-the-password-1"}
    )
    assert wrong.status_code == 422
    assert (await client.get(f"{AUTH}/me", headers=alice)).status_code == 200  # nothing happened

    resp = await client.request(
        "DELETE", f"{AUTH}/account", headers=alice, json={"password": PASSWORD}
    )
    assert resp.status_code == 202

    # locked immediately: no login, old token dead, documents invisible
    login = await client.post(
        f"{AUTH}/login", json={"email": "alice@example.com", "password": PASSWORD}
    )
    assert login.status_code == 401
    assert (await client.get(f"{AUTH}/me", headers=alice)).status_code == 401
    # (the account cannot register again until the purge removed the row: the email is still held)
    dup = await client.post(
        f"{AUTH}/register", json={"email": "alice@example.com", "password": PASSWORD}
    )
    assert dup.status_code == 409  # the row still exists, so the address is still held

    # the worker purges vectors, files and finally the row
    async def gone() -> bool:
        dispatch_now(infra)
        return (
            await scalar(infra, "SELECT count(*) FROM users WHERE id = :i", i=uuid.UUID(alice_id))
            == 0
        )

    await wait_until(gone)
    assert (
        await scalar(
            infra, "SELECT count(*) FROM documents WHERE user_id = :i", i=uuid.UUID(alice_id)
        )
        == 0
    )
    assert (
        await scalar(
            infra, "SELECT count(*) FROM refresh_tokens WHERE user_id = :i", i=uuid.UUID(alice_id)
        )
        == 0
    )
    assert points_for(user_id=alice_id) == []
    assert stored_object_count() == 1  # only Bob's file is left
    assert any("being deleted" in m["Subject"] for m in read_outbox(get_settings().email_file_dir))

    # Bob is untouched
    assert len(points_for(bob_doc)) >= 1
    assert (await client.get(f"{API}/documents/{bob_doc}", headers=bob)).status_code == 200

    # the email address is free again
    again = await client.post(
        f"{AUTH}/register", json={"email": "alice@example.com", "password": PASSWORD}
    )
    assert again.status_code == 201


async def test_account_deletion_needs_the_second_factor_when_mfa_is_on(
    client: httpx2.AsyncClient, infra: Infrastructure, runtime: WorkerRuntime
) -> None:
    headers = await new_user(client)
    secret = (await client.post(f"{AUTH}/mfa/setup", headers=headers)).json()["secret"]
    confirm = await client.post(
        f"{AUTH}/mfa/confirm", headers=headers, json={"code": totp_now(secret)}
    )
    assert confirm.status_code == 200

    no_code = await client.request(
        "DELETE", f"{AUTH}/account", headers=headers, json={"password": PASSWORD}
    )
    assert no_code.status_code == 401
    assert no_code.json()["error"]["code"] == "INVALID_MFA_CODE"
    bad_code = await client.request(
        "DELETE", f"{AUTH}/account", headers=headers, json={"password": PASSWORD, "code": "000000"}
    )
    assert bad_code.status_code == 401
    assert (await client.get(f"{AUTH}/me", headers=headers)).status_code == 200  # still alive

    ok = await client.request(
        "DELETE",
        f"{AUTH}/account",
        headers=headers,
        json={"password": PASSWORD, "code": totp_now(secret, 1)},
    )
    assert ok.status_code == 202
    assert (await client.get(f"{AUTH}/me", headers=headers)).status_code == 401

    async def gone() -> bool:
        dispatch_now(infra)
        return await scalar(infra, "SELECT count(*) FROM users") == 0

    await wait_until(gone)
    assert await scalar(infra, "SELECT count(*) FROM user_mfa") == 0
    assert await scalar(infra, "SELECT count(*) FROM mfa_recovery_codes") == 0


async def test_deletion_requires_authentication_and_a_body(client: httpx2.AsyncClient) -> None:
    assert (
        await client.request("DELETE", f"{AUTH}/account", json={"password": "x"})
    ).status_code == 401
    headers = await new_user(client)
    assert (await client.request("DELETE", f"{AUTH}/account", headers=headers)).status_code == 422


async def test_reconciler_republishes_an_unfinished_account_purge(
    client: httpx2.AsyncClient, infra: Infrastructure, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def no_kick(_infra: Infrastructure) -> None:
        return None

    # no dispatcher runs, so the (real) worker cannot finish the purge before the reconciler looks
    monkeypatch.setattr("app.api.v1.routers.account.kick_dispatcher", no_kick)
    headers = await new_user(client)
    await client.request("DELETE", f"{AUTH}/account", headers=headers, json={"password": PASSWORD})
    async with infra.session_factory() as session:
        await session.execute(text("UPDATE users SET deleted_at = now() - interval '5 minutes'"))
        await session.execute(
            text("UPDATE outbox_events SET created_at = now() - interval '5 minutes'")
        )
        await session.commit()
    with infra.sync_session_factory() as session:
        report = reconcile(session, get_settings())
    assert report.user_purges_requeued == 1


async def test_deletion_request_locks_the_account_atomically_before_any_purge_runs(
    client: httpx2.AsyncClient, infra: Infrastructure, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Everything in the request transaction is visible BEFORE the worker does anything."""

    async def no_kick(_infra: Infrastructure) -> None:
        return None

    monkeypatch.setattr("app.api.v1.routers.account.kick_dispatcher", no_kick)
    headers = await new_user(client)
    login = (
        await client.post(
            f"{AUTH}/login", json={"email": "alice@example.com", "password": PASSWORD}
        )
    ).json()
    for tag in ("one", "two"):
        await upload(client, headers, sample_text_pdf(f"lock-{tag}"))

    resp = await client.request(
        "DELETE", f"{AUTH}/account", headers=headers, json={"password": PASSWORD}
    )
    assert resp.status_code == 202

    assert await scalar(infra, "SELECT is_active FROM users") is False
    assert await scalar(infra, "SELECT deleted_at IS NOT NULL FROM users") is True
    # every session was revoked (not merely hidden by the deactivation)
    assert await scalar(infra, "SELECT count(*) FROM refresh_tokens WHERE revoked_at IS NULL") == 0
    assert (
        await scalar(
            infra,
            "SELECT count(DISTINCT revoked_reason) FROM refresh_tokens "
            "WHERE revoked_reason = 'account_deleted'",
        )
        == 1
    )
    refresh = await client.post(f"{AUTH}/refresh", json={"refresh_token": login["refresh_token"]})
    assert refresh.status_code == 401
    # both documents were tombstoned and one purge event per document plus one for the user exists
    assert await scalar(infra, "SELECT count(*) FROM documents WHERE deleted_at IS NOT NULL") == 2
    assert (
        await scalar(
            infra,
            "SELECT count(*) FROM outbox_events WHERE kind = 'purge_document' AND sent_at IS NULL",
        )
        == 2
    )
    assert (
        await scalar(
            infra,
            "SELECT count(*) FROM outbox_events WHERE kind = 'purge_user' AND sent_at IS NULL",
        )
        == 1
    )
    # outstanding one-time tokens (verification email sent at registration) are dead
    assert await scalar(infra, "SELECT count(*) FROM one_time_tokens WHERE used_at IS NULL") == 0


async def test_purge_user_alone_removes_vectors_files_and_the_row(
    client: httpx2.AsyncClient,
    infra: Infrastructure,
    runtime: WorkerRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The user-level purge must not depend on the per-document purges having run."""
    from app.ingestion.purge import purge_user

    headers = await new_user(client)
    doc = (await upload(client, headers, sample_text_pdf("solo-purge"))).json()["document"]["id"]
    await wait_for_status(client, headers, doc, {"completed"})
    user_id = uuid.UUID(await scalar(infra, "SELECT id::text FROM users"))
    assert points_for(doc) and stored_object_count() == 1

    async def no_kick(_infra: Infrastructure) -> None:
        return None

    monkeypatch.setattr("app.api.v1.routers.account.kick_dispatcher", no_kick)
    await client.request("DELETE", f"{AUTH}/account", headers=headers, json={"password": PASSWORD})
    assert points_for(doc)  # nothing purged yet: no dispatcher ran
    assert await asyncio.to_thread(purge_user, runtime, user_id) == "purged"
    assert points_for(doc) == []
    assert stored_object_count() == 0
    assert await scalar(infra, "SELECT count(*) FROM users") == 0
    assert await asyncio.to_thread(purge_user, runtime, user_id) == "already_purged"  # idempotent


class _SlowMail:
    """aiosmtpd handler that takes a long time to accept each message."""

    def __init__(self, delay_s: float) -> None:
        self.delay_s = delay_s
        self.finished_at: list[float] = []

    async def handle_DATA(self, server: Any, session: Any, envelope: Any) -> str:
        await asyncio.sleep(self.delay_s)
        self.finished_at.append(time.monotonic())
        return "250 OK"


class _ResponseClock:
    """ASGI wrapper recording when the HTTP response was completely sent to the client."""

    def __init__(self, app: Any) -> None:
        self.app = app
        self.sent_at: float | None = None

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        async def clocked(message: Any) -> None:
            if message["type"] == "http.response.body" and not message.get("more_body"):
                self.sent_at = time.monotonic()
            await send(message)

        await self.app(scope, receive, clocked)


async def test_a_slow_mail_server_never_delays_the_http_response(
    infra: Infrastructure,
) -> None:
    import socket

    from aiosmtpd.controller import Controller

    from tests.integration.conftest import build_app

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
    slow = _SlowMail(delay_s=2.5)
    controller = Controller(slow, hostname="127.0.0.1", port=port)
    controller.start()
    try:
        settings = get_settings().model_copy(
            update={
                "email_backend": "smtp",
                "smtp_host": "127.0.0.1",
                "smtp_port": port,
                "smtp_starttls": False,
            }
        )
        own = Infrastructure.create(settings)
        clock = _ResponseClock(build_app(settings, own))
        transport = httpx2.ASGITransport(app=clock)
        try:
            async with httpx2.AsyncClient(transport=transport, base_url="http://test") as c:
                headers = await new_user(c)
                slow.finished_at.clear()
                resp = await c.post(
                    f"{AUTH}/password/change",
                    headers=headers,
                    json={
                        "current_password": PASSWORD,
                        "new_password": "another-long-passphrase-2026",
                    },
                )
                assert resp.status_code == 204
                assert clock.sent_at is not None
                for _ in range(100):  # the notification is delivered later, in the background
                    if slow.finished_at:
                        break
                    await asyncio.sleep(0.1)
                assert slow.finished_at, "the notification email was never delivered"
                # the response left the server well before the slow mail server finished
                assert slow.finished_at[-1] - clock.sent_at >= 1.5
        finally:
            await own.close()
    finally:
        controller.stop()
