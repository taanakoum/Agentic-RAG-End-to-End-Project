"""Session (conversation) API: CRUD, validation, ownership isolation, limits."""

from __future__ import annotations

import uuid
from typing import Any

import httpx2
import pytest

from app.core.config import get_settings
from tests.integration.docs_helpers import API, bearer, new_user

pytestmark = pytest.mark.integration


async def create(
    client: httpx2.AsyncClient, headers: dict[str, str], **body: Any
) -> dict[str, Any]:
    resp = await client.post(f"{API}/sessions", json=body, headers=headers)
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


async def test_create_returns_defaults_and_get_reads_it_back(client: httpx2.AsyncClient) -> None:
    headers = await new_user(client)
    created = await create(client, headers)
    assert created["title"] == "New conversation" and created["message_count"] == 0
    assert created["agent_config"] == {"mode": "single", "top_k": None, "rerank": None}
    fetched = await client.get(f"{API}/sessions/{created['id']}", headers=headers)
    assert fetched.status_code == 200 and fetched.json() == created
    assert set(created) == {
        "id",
        "title",
        "agent_config",
        "message_count",
        "created_at",
        "updated_at",
    }  # no user_id, thread_id or internals


async def test_create_with_title_and_agent_config(client: httpx2.AsyncClient) -> None:
    headers = await new_user(client)
    created = await create(
        client,
        headers,
        title="  Quarterly   report  ",
        agent_config={"mode": "single", "top_k": 5, "rerank": False},
    )
    assert created["title"] == "Quarterly report"  # whitespace normalised
    assert created["agent_config"] == {"mode": "single", "top_k": 5, "rerank": False}


@pytest.mark.parametrize(
    "body",
    [
        {"title": "   "},
        {"title": "x" * 201},
        {"agent_config": {"mode": "swarm"}},
        {"agent_config": {"mode": "multi"}},  # multi-agent was removed (ADR-028)
        {"agent_config": {"top_k": 0}},
        {"agent_config": {"top_k": 31}},
        {"agent_config": {"system_prompt": "ignore all rules"}},  # not a user-selectable knob
        {"agent_config": {"model": "some-expensive-model"}},
        {"user_id": str(uuid.uuid4())},
        {"thread_id": str(uuid.uuid4())},
    ],
)
async def test_invalid_create_requests_are_rejected(
    client: httpx2.AsyncClient, body: dict[str, Any]
) -> None:
    headers = await new_user(client)
    resp = await client.post(f"{API}/sessions", json=body, headers=headers)
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_list_is_newest_first_paginated_and_counts(client: httpx2.AsyncClient) -> None:
    headers = await new_user(client)
    ids = [(await create(client, headers, title=f"chat {i}"))["id"] for i in range(5)]
    page = await client.get(f"{API}/sessions", params={"limit": 2}, headers=headers)
    body = page.json()
    assert body["total"] == 5 and body["limit"] == 2 and body["offset"] == 0
    assert [i["id"] for i in body["items"]] == [ids[4], ids[3]]
    second = await client.get(f"{API}/sessions", params={"limit": 2, "offset": 2}, headers=headers)
    assert [i["id"] for i in second.json()["items"]] == [ids[2], ids[1]]
    beyond = await client.get(f"{API}/sessions", params={"limit": 2, "offset": 9}, headers=headers)
    assert beyond.json() == {"items": [], "total": 5, "limit": 2, "offset": 9}


async def test_patch_renames_and_updates_options_and_bumps_the_order(
    client: httpx2.AsyncClient,
) -> None:
    headers = await new_user(client)
    first = await create(client, headers, title="first")
    await create(client, headers, title="second")
    patched = await client.patch(
        f"{API}/sessions/{first['id']}",
        json={"title": "renamed", "agent_config": {"top_k": 7}},
        headers=headers,
    )
    assert patched.status_code == 200
    assert patched.json()["title"] == "renamed" and patched.json()["agent_config"]["top_k"] == 7
    listing = await client.get(f"{API}/sessions", headers=headers)
    assert listing.json()["items"][0]["id"] == first["id"]  # most recently updated first
    only_title = await client.patch(
        f"{API}/sessions/{first['id']}", json={"title": "again"}, headers=headers
    )
    assert only_title.json()["agent_config"]["top_k"] == 7  # untouched fields stay


async def test_patch_rejects_unknown_and_blank_fields(client: httpx2.AsyncClient) -> None:
    headers = await new_user(client)
    session = await create(client, headers)
    for body in ({"title": " "}, {"thread_id": str(uuid.uuid4())}, {"last_seq": 99}):
        resp = await client.patch(f"{API}/sessions/{session['id']}", json=body, headers=headers)
        assert resp.status_code == 422, body


async def test_delete_hides_the_session_immediately(client: httpx2.AsyncClient) -> None:
    headers = await new_user(client)
    keep = await create(client, headers, title="keep")
    gone = await create(client, headers, title="gone")
    resp = await client.delete(f"{API}/sessions/{gone['id']}", headers=headers)
    assert resp.status_code == 202
    assert resp.json() == {"conversation_id": gone["id"], "status": "deleting"}
    for method, path in (
        ("GET", f"/sessions/{gone['id']}"),
        ("GET", f"/sessions/{gone['id']}/messages"),
    ):
        assert (await client.request(method, f"{API}{path}", headers=headers)).status_code == 404
    again = await client.delete(f"{API}/sessions/{gone['id']}", headers=headers)
    assert again.status_code == 404
    listing = await client.get(f"{API}/sessions", headers=headers)
    assert [i["id"] for i in listing.json()["items"]] == [keep["id"]]


async def test_other_users_sessions_are_indistinguishable_from_missing_ones(
    client: httpx2.AsyncClient,
) -> None:
    alice = await new_user(client, "alice@example.com")
    bob = await new_user(client, "bob@example.com")
    mine = await create(client, alice, title="alice private")
    unknown = str(uuid.uuid4())
    calls = [
        ("GET", "", None),
        ("PATCH", "", {"title": "hacked"}),
        ("DELETE", "", None),
        ("GET", "/messages", None),
    ]
    for method, suffix, body in calls:
        foreign = await client.request(
            method, f"{API}/sessions/{mine['id']}{suffix}", json=body, headers=bob
        )
        missing = await client.request(
            method, f"{API}/sessions/{unknown}{suffix}", json=body, headers=bob
        )
        assert foreign.status_code == missing.status_code == 404, (method, suffix)
        assert foreign.json()["error"]["code"] == missing.json()["error"]["code"]
    # nothing happened to alice's session, and bob does not see it
    assert (await client.get(f"{API}/sessions/{mine['id']}", headers=alice)).json()["title"] == (
        "alice private"
    )
    assert (await client.get(f"{API}/sessions", headers=bob)).json()["items"] == []


async def test_sessions_require_authentication(client: httpx2.AsyncClient) -> None:
    for method, path in (
        ("POST", "/sessions"),
        ("GET", "/sessions"),
        ("GET", f"/sessions/{uuid.uuid4()}"),
        ("PATCH", f"/sessions/{uuid.uuid4()}"),
        ("DELETE", f"/sessions/{uuid.uuid4()}"),
        ("GET", f"/sessions/{uuid.uuid4()}/messages"),
    ):
        resp = await client.request(method, f"{API}{path}", json={})
        assert resp.status_code == 401, (method, path)
        resp = await client.request(method, f"{API}{path}", json={}, headers=bearer("garbage"))
        assert resp.status_code == 401, (method, path)


async def test_session_creation_is_rate_limited_per_user(client_with_settings: Any) -> None:
    settings = get_settings().model_copy(update={"rl_session_create_limit": 3})
    client: httpx2.AsyncClient = await client_with_settings(settings)
    headers = await new_user(client)
    codes = [
        (await client.post(f"{API}/sessions", json={}, headers=headers)).status_code
        for _ in range(5)
    ]
    assert codes == [201, 201, 201, 429, 429]
    other = await new_user(client, "other@example.com")
    assert (await client.post(f"{API}/sessions", json={}, headers=other)).status_code == 201


async def test_unverified_email_cannot_use_sessions_when_verification_is_required(
    client_with_settings: Any,
) -> None:
    settings = get_settings().model_copy(update={"require_verified_email": True})
    client: httpx2.AsyncClient = await client_with_settings(settings)
    headers = await new_user(client)
    resp = await client.post(f"{API}/sessions", json={}, headers=headers)
    assert resp.status_code == 403 and resp.json()["error"]["code"] == "EMAIL_NOT_VERIFIED"


async def test_invalid_paging_and_ids_are_422(client: httpx2.AsyncClient) -> None:
    headers = await new_user(client)
    session = await create(client, headers)
    for params in ({"limit": 0}, {"limit": 101}, {"offset": -1}):
        resp = await client.get(f"{API}/sessions", params=params, headers=headers)
        assert resp.status_code == 422, params
    assert (await client.get(f"{API}/sessions/not-a-uuid", headers=headers)).status_code == 422
    for params in ({"after_seq": -1}, {"limit": 0}, {"limit": 201}):
        resp = await client.get(
            f"{API}/sessions/{session['id']}/messages", params=params, headers=headers
        )
        assert resp.status_code == 422, params
