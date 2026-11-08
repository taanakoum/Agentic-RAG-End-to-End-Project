"""Ownership / isolation tests on a real owned resource: login sessions."""

from __future__ import annotations

import uuid
from typing import Any

import httpx2
import pytest

from tests.helpers import unverified_claims

pytestmark = pytest.mark.integration

PASSWORD = "correct-horse-battery-staple"
API = "/api/v1/auth"


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def make_user(
    client: httpx2.AsyncClient, email: str, logins: int = 1
) -> list[dict[str, Any]]:
    resp = await client.post(f"{API}/register", json={"email": email, "password": PASSWORD})
    assert resp.status_code == 201
    out: list[dict[str, Any]] = []
    for i in range(logins):
        r = await client.post(
            f"{API}/login",
            json={"email": email, "password": PASSWORD},
            headers={"User-Agent": f"test-device-{i}"},
        )
        assert r.status_code == 200
        out.append(r.json())
    return out


async def test_user_lists_only_own_sessions(client: httpx2.AsyncClient) -> None:
    alice = await make_user(client, "alice@example.com", logins=2)
    bob = await make_user(client, "bob@example.com", logins=1)

    mine = await client.get(f"{API}/sessions", headers=bearer(alice[0]["access_token"]))
    assert mine.status_code == 200
    sessions = mine.json()
    assert len(sessions) == 2
    assert {s["user_agent"] for s in sessions} == {"test-device-0", "test-device-1"}
    current = [s for s in sessions if s["current"]]
    assert len(current) == 1
    assert current[0]["id"] == unverified_claims(alice[0]["access_token"])["sid"]
    assert all(
        set(s) == {"id", "created_at", "last_used_at", "user_agent", "current"} for s in sessions
    )

    theirs = await client.get(f"{API}/sessions", headers=bearer(bob[0]["access_token"]))
    assert len(theirs.json()) == 1
    bob_ids = {s["id"] for s in theirs.json()}
    assert bob_ids.isdisjoint({s["id"] for s in sessions})


async def test_user_cannot_revoke_another_users_session(client: httpx2.AsyncClient) -> None:
    alice = await make_user(client, "alice@example.com")
    bob = await make_user(client, "bob@example.com")
    bob_sid = unverified_claims(bob[0]["access_token"])["sid"]

    attack = await client.delete(
        f"{API}/sessions/{bob_sid}", headers=bearer(alice[0]["access_token"])
    )
    missing = await client.delete(
        f"{API}/sessions/{uuid.uuid4()}", headers=bearer(alice[0]["access_token"])
    )
    # Someone else's session looks exactly like a session that does not exist.
    assert attack.status_code == missing.status_code == 404
    a, m = attack.json()["error"], missing.json()["error"]
    assert (a["code"], a["message"], a["details"]) == (m["code"], m["message"], m["details"])
    assert a["code"] == "RESOURCE_NOT_FOUND"

    # Bob's session is untouched and still works (access + refresh).
    assert (
        await client.get(f"{API}/me", headers=bearer(bob[0]["access_token"]))
    ).status_code == 200
    refreshed = await client.post(f"{API}/refresh", json={"refresh_token": bob[0]["refresh_token"]})
    assert refreshed.status_code == 200


async def test_user_can_revoke_own_other_session(client: httpx2.AsyncClient) -> None:
    alice = await make_user(client, "alice@example.com", logins=2)
    other_sid = unverified_claims(alice[1]["access_token"])["sid"]

    resp = await client.delete(
        f"{API}/sessions/{other_sid}", headers=bearer(alice[0]["access_token"])
    )
    assert resp.status_code == 204
    gone = await client.get(f"{API}/me", headers=bearer(alice[1]["access_token"]))
    assert gone.status_code == 401
    assert gone.json()["error"]["code"] == "SESSION_REVOKED"
    assert (
        await client.post(f"{API}/refresh", json={"refresh_token": alice[1]["refresh_token"]})
    ).status_code == 401
    # the current session is unaffected and the revoked one is no longer listed
    listed = await client.get(f"{API}/sessions", headers=bearer(alice[0]["access_token"]))
    assert [s["id"] for s in listed.json()] == [unverified_claims(alice[0]["access_token"])["sid"]]
    # revoking it again is a 404, not a silent success
    again = await client.delete(
        f"{API}/sessions/{other_sid}", headers=bearer(alice[0]["access_token"])
    )
    assert again.status_code == 404


async def test_revoking_current_session_ends_it(client: httpx2.AsyncClient) -> None:
    (alice,) = await make_user(client, "alice@example.com")
    sid = unverified_claims(alice["access_token"])["sid"]
    assert (
        await client.delete(f"{API}/sessions/{sid}", headers=bearer(alice["access_token"]))
    ).status_code == 204
    assert (await client.get(f"{API}/me", headers=bearer(alice["access_token"]))).status_code == 401


async def test_sessions_require_authentication_and_valid_ids(client: httpx2.AsyncClient) -> None:
    assert (await client.get(f"{API}/sessions")).status_code == 401
    assert (await client.delete(f"{API}/sessions/{uuid.uuid4()}")).status_code == 401
    (alice,) = await make_user(client, "alice@example.com")
    bad = await client.delete(f"{API}/sessions/not-a-uuid", headers=bearer(alice["access_token"]))
    assert bad.status_code == 422
    assert bad.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_refresh_token_of_one_user_cannot_be_used_to_log_out_anothers_session(
    client: httpx2.AsyncClient,
) -> None:
    alice = await make_user(client, "alice@example.com")
    bob = await make_user(client, "bob@example.com")
    # A refresh token is a bearer credential for its own family only.
    await client.post(f"{API}/logout", json={"refresh_token": alice[0]["refresh_token"]})
    assert (
        await client.get(f"{API}/me", headers=bearer(bob[0]["access_token"]))
    ).status_code == 200
    assert (
        await client.get(f"{API}/me", headers=bearer(alice[0]["access_token"]))
    ).status_code == 401
