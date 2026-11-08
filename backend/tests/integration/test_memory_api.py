"""Long-term memory API with a real worker, PostgreSQL and Qdrant (HashEmbedder as the model)."""

from __future__ import annotations

import uuid
from typing import Any

import httpx2
import pytest
from sqlalchemy import text

from app.core.config import get_settings
from app.infra.container import Infrastructure
from app.ingestion.runtime import WorkerRuntime
from app.models.memory import Memory
from tests.fakes import HashEmbedder
from tests.integration.docs_helpers import API, bearer, new_user, scalar
from tests.integration.memory_helpers import (
    add_memory,
    eventually,
    user_id_of,
    vector_count,
    vector_ids,
)

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _worker(runtime: WorkerRuntime, embedder: HashEmbedder) -> None:
    return None


async def indexed(infra: Infrastructure, memory_id: str) -> bool:
    return (
        await scalar(infra, "SELECT index_state FROM memories WHERE id = :i", i=memory_id)
        == "indexed"
    )


# ---------------------------------------------------------------- preferences (consent, retention)


async def test_memory_is_off_by_default_and_preferences_can_be_changed(
    client: httpx2.AsyncClient,
) -> None:
    headers = await new_user(client)
    default = await client.get(f"{API}/users/me/preferences", headers=headers)
    assert default.json() == {"memory_enabled": False, "memory_retention_days": 365}
    changed = await client.patch(
        f"{API}/users/me/preferences",
        json={"memory_enabled": True, "memory_retention_days": 30},
        headers=headers,
    )
    assert changed.json() == {"memory_enabled": True, "memory_retention_days": 30}
    partial = await client.patch(
        f"{API}/users/me/preferences", json={"memory_enabled": False}, headers=headers
    )
    assert partial.json() == {"memory_enabled": False, "memory_retention_days": 30}


@pytest.mark.parametrize(
    "body",
    [
        {"memory_retention_days": 0},
        {"memory_retention_days": 3651},
        {"theme": "dark"},
        {"memory_enabled": "maybe"},
    ],
)
async def test_invalid_preferences_are_rejected(
    client: httpx2.AsyncClient, body: dict[str, Any]
) -> None:
    headers = await new_user(client)
    resp = await client.patch(f"{API}/users/me/preferences", json=body, headers=headers)
    assert resp.status_code == 422


async def test_preferences_are_per_user(client: httpx2.AsyncClient) -> None:
    alice = await new_user(client, "alice@example.com")
    bob = await new_user(client, "bob@example.com")
    await client.patch(f"{API}/users/me/preferences", json={"memory_enabled": True}, headers=alice)
    assert (await client.get(f"{API}/users/me/preferences", headers=bob)).json()[
        "memory_enabled"
    ] is False


# ------------------------------------------------------------------ create / list


async def test_a_created_memory_is_stored_listed_and_indexed_by_the_worker(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    headers = await new_user(client)
    user = await user_id_of(client, headers)
    created = await add_memory(client, headers, "The user prefers dark mode.", importance=4)
    assert created["kind"] == "semantic" and created["source"] == "user"
    assert created["importance"] == 4 and created["last_used_at"] is None
    assert set(created) == {
        "id",
        "kind",
        "content",
        "importance",
        "source",
        "created_at",
        "updated_at",
        "last_used_at",
    }
    listing = await client.get(f"{API}/memory", headers=headers)
    assert [m["id"] for m in listing.json()["items"]] == [created["id"]]
    await eventually(lambda: indexed(infra, created["id"]))
    assert vector_ids(user) == {created["id"]}  # the vector uses the memory id as its point id


async def test_content_is_normalised_and_validated(client: httpx2.AsyncClient) -> None:
    headers = await new_user(client)
    created = await add_memory(client, headers, "  Likes   short   answers  ")
    assert created["content"] == "Likes short answers"
    for body in (
        {"content": ""},
        {"content": "   "},
        {"content": "x" * 5001},
        {"content": "ok", "kind": "dream"},
        {"content": "ok", "importance": 0},
        {"content": "ok", "importance": 6},
        {"content": "ok", "user_id": str(uuid.uuid4())},
        {"content": "ok", "source": "consolidation"},
    ):
        resp = await client.post(f"{API}/memory", json=body, headers=headers)
        assert resp.status_code == 422, body


async def test_overlong_content_follows_the_configured_limit(client_with_settings: Any) -> None:
    settings = get_settings().model_copy(update={"memory_max_chars": 40})
    client: httpx2.AsyncClient = await client_with_settings(settings)
    headers = await new_user(client)
    resp = await client.post(f"{API}/memory", json={"content": "y" * 41}, headers=headers)
    assert resp.status_code == 422
    assert resp.json()["error"]["details"][0]["code"] == "content_too_long"


@pytest.mark.parametrize(
    "content",
    [
        "my password is hunter2",
        "reach me at dana@example.com",
        "call +1 (415) 555-0132",
        "card 4111 1111 1111 1111",
        "ssn 123-45-6789",
        "api key sk-ABCDEF0123456789abcdef01",
    ],
)
async def test_sensitive_data_is_refused(client: httpx2.AsyncClient, content: str) -> None:
    headers = await new_user(client)
    resp = await client.post(f"{API}/memory", json={"content": content}, headers=headers)
    assert resp.status_code == 422
    assert resp.json()["error"]["details"][0]["code"] == "sensitive_content"
    assert content not in resp.text  # the refused text is not echoed back
    assert (await client.get(f"{API}/memory", headers=headers)).json()["total"] == 0


async def test_the_same_fact_cannot_be_saved_twice_but_can_after_deleting(
    client: httpx2.AsyncClient,
) -> None:
    headers = await new_user(client)
    first = await add_memory(client, headers, "Deploys happen on Fridays.")
    dup = await client.post(
        f"{API}/memory", json={"content": "  deploys HAPPEN on   fridays. "}, headers=headers
    )
    assert dup.status_code == 409 and dup.json()["error"]["code"] == "DUPLICATE_RESOURCE"
    assert (await client.delete(f"{API}/memory/{first['id']}", headers=headers)).status_code == 204
    again = await add_memory(client, headers, "Deploys happen on Fridays.")
    assert again["id"] != first["id"]


async def test_another_user_can_save_the_same_text(client: httpx2.AsyncClient) -> None:
    alice = await new_user(client, "alice@example.com")
    bob = await new_user(client, "bob@example.com")
    a = await add_memory(client, alice, "Prefers metric units.")
    b = await add_memory(client, bob, "Prefers metric units.")
    assert a["id"] != b["id"]


async def test_the_number_of_memories_per_user_is_capped(client_with_settings: Any) -> None:
    settings = get_settings().model_copy(update={"memory_max_per_user": 2})
    client: httpx2.AsyncClient = await client_with_settings(settings)
    headers = await new_user(client)
    await add_memory(client, headers, "fact one")
    await add_memory(client, headers, "fact two")
    resp = await client.post(f"{API}/memory", json={"content": "fact three"}, headers=headers)
    assert resp.status_code == 422
    assert resp.json()["error"]["details"][0]["code"] == "memory_limit_reached"


async def test_list_filters_by_kind_and_paginates_newest_first(
    client: httpx2.AsyncClient,
) -> None:
    headers = await new_user(client)
    ids = [(await add_memory(client, headers, f"semantic fact {i}"))["id"] for i in range(4)]
    episodic = await add_memory(client, headers, "Met the team in March.", kind="episodic")
    page = (await client.get(f"{API}/memory", params={"limit": 2}, headers=headers)).json()
    assert page["total"] == 5 and page["items"][0]["id"] == episodic["id"]
    only = (
        await client.get(f"{API}/memory", params={"kind": "semantic", "limit": 3}, headers=headers)
    ).json()
    assert only["total"] == 4 and [m["id"] for m in only["items"]] == ids[::-1][:3]
    only_episodic = await client.get(f"{API}/memory", params={"kind": "episodic"}, headers=headers)
    assert [m["id"] for m in only_episodic.json()["items"]] == [episodic["id"]]
    for params in ({"kind": "dream"}, {"limit": 0}, {"limit": 101}, {"offset": -1}):
        assert (
            await client.get(f"{API}/memory", params=params, headers=headers)
        ).status_code == 422


# ------------------------------------------------------------------ update


async def test_editing_a_memory_reindexes_it_and_search_follows_the_new_text(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    headers = await new_user(client)
    created = await add_memory(client, headers, "The user prefers dark mode.")
    await eventually(lambda: indexed(infra, created["id"]))
    patched = await client.patch(
        f"{API}/memory/{created['id']}",
        json={
            "content": "The user prefers vegetarian restaurants.",
            "importance": 5,
            "kind": "episodic",
        },
        headers=headers,
    )
    assert patched.status_code == 200
    body = patched.json()
    assert body["content"] == "The user prefers vegetarian restaurants."
    assert body["importance"] == 5 and body["kind"] == "episodic"
    await eventually(lambda: indexed(infra, created["id"]))
    new = await client.post(
        f"{API}/memory/search", json={"query": "vegetarian restaurants"}, headers=headers
    )
    old = await client.post(f"{API}/memory/search", json={"query": "dark mode"}, headers=headers)
    assert [r["memory"]["id"] for r in new.json()["results"]] == [created["id"]]
    assert old.json()["results"] == []


async def test_importance_only_edits_do_not_touch_the_index(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    headers = await new_user(client)
    created = await add_memory(client, headers, "Uses vim.")
    await eventually(lambda: indexed(infra, created["id"]))
    events_before = await scalar(
        infra, "SELECT count(*) FROM outbox_events WHERE kind = 'index_memory'"
    )
    await client.patch(f"{API}/memory/{created['id']}", json={"importance": 1}, headers=headers)
    events_after = await scalar(
        infra, "SELECT count(*) FROM outbox_events WHERE kind = 'index_memory'"
    )
    assert events_after == events_before


async def test_edit_validation_duplicates_and_ownership(client: httpx2.AsyncClient) -> None:
    alice = await new_user(client, "alice@example.com")
    bob = await new_user(client, "bob@example.com")
    first = await add_memory(client, alice, "Fact A.")
    second = await add_memory(client, alice, "Fact B.")
    clash = await client.patch(
        f"{API}/memory/{second['id']}", json={"content": "fact a."}, headers=alice
    )
    assert clash.status_code == 409
    sensitive = await client.patch(
        f"{API}/memory/{second['id']}", json={"content": "password is hunter2"}, headers=alice
    )
    assert sensitive.status_code == 422
    for body in ({"content": ""}, {"kind": "dream"}, {"user_id": str(uuid.uuid4())}):
        resp = await client.patch(f"{API}/memory/{second['id']}", json=body, headers=alice)
        assert resp.status_code == 422, body
    foreign = await client.patch(
        f"{API}/memory/{first['id']}", json={"content": "hacked"}, headers=bob
    )
    missing = await client.patch(
        f"{API}/memory/{uuid.uuid4()}", json={"content": "hacked"}, headers=bob
    )
    assert foreign.status_code == missing.status_code == 404
    listing = await client.get(f"{API}/memory", headers=alice)
    assert {m["content"] for m in listing.json()["items"]} == {"Fact A.", "Fact B."}


# ------------------------------------------------------------------ delete / reset


async def test_deleting_a_memory_hides_it_at_once_and_the_worker_removes_vector_and_row(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    headers = await new_user(client)
    user = await user_id_of(client, headers)
    keep = await add_memory(client, headers, "Keep me, I like tea.")
    gone = await add_memory(client, headers, "Delete me, I like coffee.")
    await eventually(lambda: indexed(infra, gone["id"]))
    resp = await client.delete(f"{API}/memory/{gone['id']}", headers=headers)
    assert resp.status_code == 204
    listing = await client.get(f"{API}/memory", headers=headers)
    assert [m["id"] for m in listing.json()["items"]] == [keep["id"]]
    assert (await client.delete(f"{API}/memory/{gone['id']}", headers=headers)).status_code == 404

    async def purged() -> bool:
        return await scalar(
            infra, "SELECT count(*) FROM memories WHERE id = :i", i=gone["id"]
        ) == 0 and gone["id"] not in vector_ids(user)

    await eventually(purged)
    assert await scalar(infra, "SELECT count(*) FROM memories WHERE id = :i", i=keep["id"]) == 1


async def test_reset_requires_explicit_confirmation(client: httpx2.AsyncClient) -> None:
    headers = await new_user(client)
    await add_memory(client, headers, "Something to keep.")
    for body in ({}, {"confirm": False}, {"confirm": "yes"}, {"confirm": True, "all": True}):
        resp = await client.post(f"{API}/memory/reset", json=body, headers=headers)
        assert resp.status_code == 422, body
    assert (await client.get(f"{API}/memory", headers=headers)).json()["total"] == 1


async def test_reset_deletes_every_memory_and_vector_of_the_caller_only(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    alice = await new_user(client, "alice@example.com")
    bob = await new_user(client, "bob@example.com")
    a_user, b_user = await user_id_of(client, alice), await user_id_of(client, bob)
    a_ids = [(await add_memory(client, alice, f"alice fact {i}"))["id"] for i in range(3)]
    b_id = (await add_memory(client, bob, "bob fact"))["id"]
    for memory_id in (*a_ids, b_id):
        await eventually(lambda m=memory_id: indexed(infra, m))
    resp = await client.post(f"{API}/memory/reset", json={"confirm": True}, headers=alice)
    assert resp.status_code == 202 and resp.json() == {"status": "resetting", "memories": 3}
    assert (await client.get(f"{API}/memory", headers=alice)).json()["total"] == 0

    async def reset_done() -> bool:
        return (
            await scalar(infra, "SELECT count(*) FROM memories WHERE user_id = :u", u=a_user) == 0
            and vector_count(a_user) == 0
        )

    await eventually(reset_done)
    assert (await client.get(f"{API}/memory", headers=bob)).json()["total"] == 1
    assert vector_ids(b_user) == {b_id}


# ------------------------------------------------------------------ search (relevance only)


async def test_search_returns_only_relevant_memories_best_first(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    headers = await new_user(client)
    best = await add_memory(client, headers, "The user prefers dark mode in every editor.")
    ok = await add_memory(client, headers, "Dark mode is also wanted in the terminal.")
    other = await add_memory(client, headers, "The user is allergic to peanuts.")
    for m in (best, ok, other):
        await eventually(lambda i=m["id"]: indexed(infra, i))
    resp = await client.post(
        f"{API}/memory/search", json={"query": "dark mode in the editor"}, headers=headers
    )
    results = resp.json()["results"]
    ids = [r["memory"]["id"] for r in results]
    assert ids[0] == best["id"] and other["id"] not in ids  # irrelevant memories are not returned
    scores = [r["score"] for r in results]
    assert scores == sorted(scores, reverse=True) and all(0.5 <= s <= 1.0 for s in scores)
    unrelated = await client.post(
        f"{API}/memory/search", json={"query": "quarterly revenue forecast"}, headers=headers
    )
    assert unrelated.json()["results"] == []  # nothing relevant -> nothing injected


async def test_search_k_limits_results_and_marks_the_memories_as_used(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    headers = await new_user(client)
    items = [await add_memory(client, headers, f"likes dark mode variant {i}") for i in range(4)]
    for m in items:
        await eventually(lambda i=m["id"]: indexed(infra, i))
    before = {
        m["id"]: m["updated_at"]
        for m in (await client.get(f"{API}/memory", headers=headers)).json()["items"]
    }
    resp = await client.post(
        f"{API}/memory/search", json={"query": "dark mode", "k": 2}, headers=headers
    )
    returned = [r["memory"]["id"] for r in resp.json()["results"]]
    assert len(returned) == 2
    used = await client.get(f"{API}/memory", headers=headers)
    stamped = {m["id"] for m in used.json()["items"] if m["last_used_at"] is not None}
    assert stamped == set(returned)  # only what was retrieved counts as used
    after = {m["id"]: m["updated_at"] for m in used.json()["items"]}
    assert after == before  # being retrieved is not an edit: updated_at (retention) is unchanged


async def test_search_never_returns_another_users_memories(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    alice = await new_user(client, "alice@example.com")
    bob = await new_user(client, "bob@example.com")
    a = await add_memory(client, alice, "Alice keeps her notes in the blue notebook.")
    b = await add_memory(client, bob, "Bob keeps his notes in the blue notebook.")
    for m in (a, b):
        await eventually(lambda i=m["id"]: indexed(infra, i))
    for headers, own in ((alice, a["id"]), (bob, b["id"])):
        resp = await client.post(
            f"{API}/memory/search", json={"query": "notes in the blue notebook"}, headers=headers
        )
        assert [r["memory"]["id"] for r in resp.json()["results"]] == [own]


async def test_a_deleted_memory_is_never_returned_even_while_its_vector_still_exists(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    headers = await new_user(client)
    user = await user_id_of(client, headers)
    created = await add_memory(client, headers, "The user likes mountain biking on weekends.")
    await eventually(lambda: indexed(infra, created["id"]))
    # tombstone in the database only: the vector stays (the worker has not purged it yet)
    async with infra.session_factory() as session:
        await session.execute(
            text("UPDATE memories SET deleted_at = now() WHERE id = :i"), {"i": created["id"]}
        )
        await session.commit()
    assert vector_ids(user) == {created["id"]}
    resp = await client.post(
        f"{API}/memory/search", json={"query": "mountain biking"}, headers=headers
    )
    assert resp.json()["results"] == []


async def test_the_agent_path_only_sees_memories_when_the_user_consented(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    from app.services.auth_service import AuthenticatedUser
    from app.services.factory import build_memory_service

    headers = await new_user(client)
    user = await user_id_of(client, headers)
    created = await add_memory(client, headers, "The user prefers aisle seats on flights.")
    await eventually(lambda: indexed(infra, created["id"]))
    identity = AuthenticatedUser(id=user, email="alice@example.com", session_id=uuid.uuid4())

    async def agent_hits() -> list[Memory]:
        async with infra.session_factory() as session:
            service = build_memory_service(session, infra)
            return [h.memory for h in await service.relevant(identity, "aisle seats on flights")]

    assert await agent_hits() == []  # consent is off by default
    await client.patch(
        f"{API}/users/me/preferences", json={"memory_enabled": True}, headers=headers
    )
    assert [m.id for m in await agent_hits()] == [uuid.UUID(created["id"])]
    await client.patch(
        f"{API}/users/me/preferences", json={"memory_enabled": False}, headers=headers
    )
    assert await agent_hits() == []


# ------------------------------------------------------------------ access control and limits


async def test_memory_endpoints_require_authentication(client: httpx2.AsyncClient) -> None:
    some = uuid.uuid4()
    for method, path in (
        ("GET", "/memory"),
        ("POST", "/memory"),
        ("POST", "/memory/search"),
        ("POST", "/memory/reset"),
        ("PATCH", f"/memory/{some}"),
        ("DELETE", f"/memory/{some}"),
        ("GET", "/users/me/preferences"),
        ("PATCH", "/users/me/preferences"),
    ):
        for headers in ({}, bearer("garbage")):
            resp = await client.request(method, f"{API}{path}", json={}, headers=headers)
            assert resp.status_code == 401, (method, path)


async def test_memory_writes_are_rate_limited_per_user(client_with_settings: Any) -> None:
    settings = get_settings().model_copy(update={"rl_memory_write_limit": 3})
    client: httpx2.AsyncClient = await client_with_settings(settings)
    headers = await new_user(client)
    codes = [
        (
            await client.post(f"{API}/memory", json={"content": f"fact {i}"}, headers=headers)
        ).status_code
        for i in range(5)
    ]
    assert codes == [201, 201, 201, 429, 429]
    other = await new_user(client, "other@example.com")
    assert (
        await client.post(f"{API}/memory", json={"content": "fact"}, headers=other)
    ).status_code == 201


async def test_unverified_email_cannot_use_memory_when_verification_is_required(
    client_with_settings: Any,
) -> None:
    settings = get_settings().model_copy(update={"require_verified_email": True})
    client: httpx2.AsyncClient = await client_with_settings(settings)
    headers = await new_user(client)
    resp = await client.post(f"{API}/memory", json={"content": "fact"}, headers=headers)
    assert resp.status_code == 403 and resp.json()["error"]["code"] == "EMAIL_NOT_VERIFIED"


@pytest.mark.real_model
async def test_the_calibrated_threshold_separates_relevant_from_unrelated_with_the_real_model(
    client_with_settings: Any,
    infra: Infrastructure,
    runtime: WorkerRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.ingestion.embedding import FastEmbedEmbedder, set_embedder_factory

    settings = get_settings().model_copy(update={"memory_min_score": 0.6})
    real = FastEmbedEmbedder(settings)
    # BOTH sides must use the real model BEFORE anything is created: the worker starts indexing
    # as soon as a memory is saved, and would otherwise embed it with the test double
    monkeypatch.setattr(runtime, "embedder", real)
    set_embedder_factory(lambda _settings: real)
    try:
        client: httpx2.AsyncClient = await client_with_settings(settings)
        headers = await new_user(client)
        created = [
            await add_memory(
                client, headers, "The user prefers dark mode in every editor and terminal."
            ),
            await add_memory(client, headers, "The user is allergic to peanuts."),
            await add_memory(client, headers, "Deploys happen on Fridays at 16:00."),
        ]
        for m in created:
            await eventually(lambda i=m["id"]: indexed(infra, i), wait_s=120)

        async def ask(query: str) -> list[str]:
            resp = await client.post(f"{API}/memory/search", json={"query": query}, headers=headers)
            return [r["memory"]["content"] for r in resp.json()["results"]]

        assert await ask("should the UI default to a dark theme") == [created[0]["content"]]
        assert created[1]["content"] in await ask("does the user have any food allergies")
        assert await ask("what is the capital of France") == []
        assert await ask("write a haiku about autumn") == []
    finally:
        set_embedder_factory(None)
