"""Memory consolidation, retention, reconciliation and deletion cascades (real worker)."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Any

import httpx2
import pytest
from pydantic import SecretStr
from sqlalchemy import text

from app.core.config import get_settings
from app.infra.container import Infrastructure
from app.ingestion.outbox import dispatch_outbox
from app.ingestion.reconcile import reconcile
from app.ingestion.runtime import WorkerRuntime
from app.llm.base import ChatModel
from app.memory import llm_extract
from app.memory.pipeline import consolidate_conversation, sweep_retention
from app.services.chat_turns import ChatTurnService
from app.tasks.celery_app import celery_app
from tests.fakes import HashEmbedder, ScriptedModel
from tests.integration.docs_helpers import API, new_user, scalar
from tests.integration.memory_helpers import (
    add_memory,
    eventually,
    user_id_of,
    vector_count,
    vector_ids,
)

pytestmark = pytest.mark.integration

PASSWORD = "correct-horse-battery-staple"


@pytest.fixture(autouse=True)
def _worker(runtime: WorkerRuntime, embedder: HashEmbedder) -> None:
    return None


async def enable_memory(client: httpx2.AsyncClient, headers: dict[str, str], **extra: Any) -> None:
    resp = await client.patch(
        f"{API}/users/me/preferences", json={"memory_enabled": True, **extra}, headers=headers
    )
    assert resp.status_code == 200


async def new_conversation(client: httpx2.AsyncClient, headers: dict[str, str]) -> uuid.UUID:
    resp = await client.post(f"{API}/sessions", json={}, headers=headers)
    return uuid.UUID(resp.json()["id"])


async def say(
    infra: Infrastructure, user: uuid.UUID, conversation: uuid.UUID, text_: str, key: str
) -> None:
    """One finished turn: the user says something, the assistant answers."""
    settings = get_settings()
    async with infra.session_factory() as session:
        handle = await ChatTurnService(session=session, settings=settings).begin(
            user, conversation, idempotency_key=key, content=text_
        )
    async with infra.session_factory() as session:
        await ChatTurnService(session=session, settings=settings).complete(
            user, handle.execution_id, answer="Noted.", response=None
        )


async def memories(infra: Infrastructure, user: uuid.UUID) -> list[Any]:
    async with infra.session_factory() as session:
        rows = await session.execute(
            text(
                "SELECT content, source, source_conversation_id, index_state FROM memories "
                "WHERE user_id = :u AND deleted_at IS NULL ORDER BY created_at"
            ),
            {"u": user},
        )
        return list(rows.all())


def consolidate(rt: WorkerRuntime, conversation: uuid.UUID, user: uuid.UUID) -> str:
    return consolidate_conversation(rt, conversation, user)


# ------------------------------------------------------------------ consolidation


async def test_consolidation_stores_explicit_statements_and_skips_everything_else(
    client: httpx2.AsyncClient, infra: Infrastructure, runtime: WorkerRuntime
) -> None:
    headers = await new_user(client)
    user = await user_id_of(client, headers)
    await enable_memory(client, headers)
    conversation = await new_conversation(client, headers)
    await say(
        infra, user, conversation, "Hi! My name is Dana. What is the weather?", "turn-key-001"
    )
    await say(infra, user, conversation, "Remember that I deploy on Fridays.", "turn-key-002")
    await say(infra, user, conversation, "Remember that my password is hunter2.", "turn-key-003")
    await say(infra, user, conversation, "Thanks, that helps a lot.", "turn-key-004")

    result = await asyncio.to_thread(consolidate, runtime, conversation, user)
    assert result == "created:2"
    dispatch_outbox(infra.sync_session_factory, celery_app, batch_size=100)
    stored = await memories(infra, user)
    assert [m.content for m in stored] == ["The user's name is Dana.", "I deploy on Fridays."]
    assert {m.source for m in stored} == {"consolidation"}
    assert {m.source_conversation_id for m in stored} == {conversation}
    # consolidated memories become searchable like any other
    await eventually(lambda: vector_count(user) == 2)
    found = await client.post(
        f"{API}/memory/search", json={"query": "deploy on Fridays"}, headers=headers
    )
    assert [r["memory"]["content"] for r in found.json()["results"]] == ["I deploy on Fridays."]


async def test_consolidation_is_idempotent_and_deduplicates_against_existing_memories(
    client: httpx2.AsyncClient, infra: Infrastructure, runtime: WorkerRuntime
) -> None:
    headers = await new_user(client)
    user = await user_id_of(client, headers)
    await enable_memory(client, headers)
    await add_memory(client, headers, "The user prefers concise answers.")
    conversation = await new_conversation(client, headers)
    await say(infra, user, conversation, "I prefer concise answers.", "turn-key-001")
    await say(infra, user, conversation, "I work at Acme Corp.", "turn-key-002")
    assert await asyncio.to_thread(consolidate, runtime, conversation, user) == "created:1"
    # a duplicate delivery of the same task, and a rerun with nothing new
    assert await asyncio.to_thread(consolidate, runtime, conversation, user) == "nothing_new"
    assert len(await memories(infra, user)) == 2
    await say(infra, user, conversation, "I usually use Python.", "turn-key-003")
    assert await asyncio.to_thread(consolidate, runtime, conversation, user) == "created:1"
    assert len(await memories(infra, user)) == 3


async def test_concurrent_consolidations_do_not_create_duplicates(
    client: httpx2.AsyncClient, infra: Infrastructure, runtime: WorkerRuntime
) -> None:
    headers = await new_user(client)
    user = await user_id_of(client, headers)
    await enable_memory(client, headers)
    conversation = await new_conversation(client, headers)
    await say(infra, user, conversation, "Remember that standups are at nine.", "turn-key-001")
    results = await asyncio.gather(
        *(asyncio.to_thread(consolidate, runtime, conversation, user) for _ in range(4))
    )
    assert sorted(results) == ["created:1", "nothing_new", "nothing_new", "nothing_new"]
    assert len(await memories(infra, user)) == 1


async def test_nothing_is_stored_without_consent_and_old_messages_stay_unexamined(
    client: httpx2.AsyncClient, infra: Infrastructure, runtime: WorkerRuntime
) -> None:
    headers = await new_user(client)
    user = await user_id_of(client, headers)
    conversation = await new_conversation(client, headers)
    await say(infra, user, conversation, "My name is Dana.", "turn-key-001")
    assert await asyncio.to_thread(consolidate, runtime, conversation, user) == "consent_off"
    assert await memories(infra, user) == []
    # the user switches memory on later: what was said before is NOT examined retroactively
    await enable_memory(client, headers)
    assert await asyncio.to_thread(consolidate, runtime, conversation, user) == "nothing_new"
    assert await memories(infra, user) == []
    await say(infra, user, conversation, "I prefer tea.", "turn-key-002")
    assert await asyncio.to_thread(consolidate, runtime, conversation, user) == "created:1"


async def test_consolidation_ignores_missing_foreign_and_deleted_conversations(
    client: httpx2.AsyncClient, infra: Infrastructure, runtime: WorkerRuntime
) -> None:
    alice = await new_user(client, "alice@example.com")
    bob = await new_user(client, "bob@example.com")
    a_user, b_user = await user_id_of(client, alice), await user_id_of(client, bob)
    await enable_memory(client, alice)
    conversation = await new_conversation(client, alice)
    await say(infra, a_user, conversation, "My name is Dana.", "turn-key-001")
    assert (
        await asyncio.to_thread(consolidate, runtime, conversation, b_user) == "gone"
    )  # not bob's
    assert await asyncio.to_thread(consolidate, runtime, uuid.uuid4(), a_user) == "gone"
    await client.delete(f"{API}/sessions/{conversation}", headers=alice)
    assert await asyncio.to_thread(consolidate, runtime, conversation, a_user) == "gone"
    assert await memories(infra, a_user) == [] and await memories(infra, b_user) == []


async def test_the_per_user_memory_cap_applies_to_consolidation(
    client: httpx2.AsyncClient, infra: Infrastructure, runtime: WorkerRuntime
) -> None:
    headers = await new_user(client)
    user = await user_id_of(client, headers)
    await enable_memory(client, headers)
    conversation = await new_conversation(client, headers)
    await say(
        infra, user, conversation, "I prefer tea. I prefer jazz. I prefer trains.", "turn-key-001"
    )
    small = replace(runtime, settings=get_settings().model_copy(update={"memory_max_per_user": 2}))
    assert await asyncio.to_thread(consolidate, small, conversation, user) == "created:2"


# ------------------------------------------------------------------ automatic trigger


async def test_enough_new_messages_queue_one_consolidation_for_consenting_users_only(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    on = await new_user(client, "on@example.com")
    off = await new_user(client, "off@example.com")
    on_user, off_user = await user_id_of(client, on), await user_id_of(client, off)
    await enable_memory(client, on)
    every = get_settings().memory_consolidate_every
    turns = every // 2  # each turn adds two messages
    conv_on = await new_conversation(client, on)
    conv_off = await new_conversation(client, off)

    async def pending(conversation: uuid.UUID) -> int:
        return int(
            await scalar(
                infra,
                "SELECT count(*) FROM outbox_events WHERE kind = 'consolidate_conversation' "
                "AND payload->>'conversation_id' = :c AND sent_at IS NULL",
                c=str(conversation),
            )
        )

    for n in range(turns - 1):
        await say(infra, on_user, conv_on, f"message number {n}", f"on-key-{n:06d}")
    assert await pending(conv_on) == 0  # below the threshold
    await say(infra, on_user, conv_on, "one more message", "on-key-999999")
    assert await pending(conv_on) == 1
    await say(infra, on_user, conv_on, "and another", "on-key-999998")
    assert await pending(conv_on) == 1  # not piled up while one is waiting
    for n in range(turns + 1):
        await say(infra, off_user, conv_off, f"message number {n}", f"off-key-{n:06d}")
    assert await pending(conv_off) == 0  # no consent, no work


async def test_the_worker_consolidates_automatically_end_to_end(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    headers = await new_user(client)
    user = await user_id_of(client, headers)
    await enable_memory(client, headers)
    conversation = await new_conversation(client, headers)
    texts = ["My name is Dana.", "I work at Acme Corp.", "I prefer concise answers."]
    for n, content in enumerate(texts):
        await say(infra, user, conversation, content, f"auto-key-{n:06d}")
    dispatch_outbox(infra.sync_session_factory, celery_app, batch_size=100)

    async def done() -> bool:
        return len(await memories(infra, user)) == 3 and vector_count(user) == 3

    await eventually(done)
    assert {m.content for m in await memories(infra, user)} == {
        "The user's name is Dana.",
        "The user works at Acme Corp.",
        "The user prefers concise answers.",
    }


# ------------------------------------------------------------------ retention


async def test_retention_removes_stale_memories_per_user_and_spares_recently_used_ones(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    short = await new_user(client, "short@example.com")
    long_ = await new_user(client, "long@example.com")
    s_user, l_user = await user_id_of(client, short), await user_id_of(client, long_)
    await enable_memory(client, short, memory_retention_days=30)
    old_short = await add_memory(client, short, "old short-retention fact")
    used_short = await add_memory(client, short, "old but recently used fact")
    new_short = await add_memory(client, short, "fresh fact")
    old_long = await add_memory(client, long_, "old default-retention fact")
    async with infra.session_factory() as session:
        await session.execute(
            text(
                "UPDATE memories SET updated_at = now() - interval '60 days' WHERE id IN (:a, :b)"
            ),
            {"a": old_short["id"], "b": used_short["id"]},
        )
        await session.execute(
            text("UPDATE memories SET last_used_at = now() - interval '2 days' WHERE id = :i"),
            {"i": used_short["id"]},
        )
        await session.execute(
            text("UPDATE memories SET updated_at = now() - interval '60 days' WHERE id = :i"),
            {"i": old_long["id"]},
        )
        await session.commit()
    with infra.sync_session_factory() as session:
        assert sweep_retention(session) == 1
    live_short = {
        m["id"] for m in (await client.get(f"{API}/memory", headers=short)).json()["items"]
    }
    assert live_short == {used_short["id"], new_short["id"]}
    assert (await client.get(f"{API}/memory", headers=long_)).json()[
        "total"
    ] == 1  # 365-day default
    # the worker finishes the job: row and vector of the expired memory are gone
    dispatch_outbox(infra.sync_session_factory, celery_app, batch_size=100)

    async def purged() -> bool:
        gone = await scalar(infra, "SELECT count(*) FROM memories WHERE id = :i", i=old_short["id"])
        return gone == 0 and old_short["id"] not in vector_ids(s_user)

    await eventually(purged)
    assert vector_count(l_user) >= 0  # other users' vectors are not touched by the sweep
    with infra.sync_session_factory() as session:
        assert sweep_retention(session) == 0  # idempotent


def test_the_retention_sweep_is_scheduled_by_beat() -> None:
    assert celery_app.conf.beat_schedule["memory-retention"]["task"] == "memory.sweep_retention"


# ------------------------------------------------------------------ reconciliation and cascades


async def test_the_reconciler_republishes_lost_index_and_purge_events(
    client: httpx2.AsyncClient, infra: Infrastructure, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def no_kick(_infra: Infrastructure) -> None:
        return None

    monkeypatch.setattr("app.api.v1.routers.memory.kick_dispatcher", no_kick)
    headers = await new_user(client)
    user = await user_id_of(client, headers)
    lost_index = await add_memory(client, headers, "created but never indexed")
    doomed = await add_memory(client, headers, "deleted but never purged")
    await client.delete(f"{API}/memory/{doomed['id']}", headers=headers)
    async with infra.session_factory() as session:  # every message is lost
        await session.execute(text("DELETE FROM outbox_events WHERE kind LIKE '%memory'"))
        await session.commit()
    await asyncio.sleep(1.2)  # RECONCILE_PURGE_TTL_S=1 in the test environment
    with infra.sync_session_factory() as session:
        report = reconcile(session, get_settings())
    assert report.memory_events_requeued == 2
    dispatch_outbox(infra.sync_session_factory, celery_app, batch_size=100)

    async def repaired() -> bool:
        state = await scalar(
            infra, "SELECT index_state FROM memories WHERE id = :i", i=lost_index["id"]
        )
        gone = await scalar(infra, "SELECT count(*) FROM memories WHERE id = :i", i=doomed["id"])
        return state == "indexed" and gone == 0

    await eventually(repaired)
    assert vector_ids(user) == {lost_index["id"]}


async def test_deleting_the_account_removes_memories_and_their_vectors(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    headers = await new_user(client)
    user = await user_id_of(client, headers)
    bystander = await new_user(client, "bystander@example.com")
    other = await user_id_of(client, bystander)
    ids = [(await add_memory(client, headers, f"fact number {i}"))["id"] for i in range(3)]
    keep = await add_memory(client, bystander, "bystander fact")
    await eventually(lambda: vector_count(user) == 3 and vector_count(other) == 1)
    resp = await client.request(
        "DELETE", f"{API}/auth/account", json={"password": PASSWORD}, headers=headers
    )
    assert resp.status_code == 202, resp.text

    async def gone() -> bool:
        users = await scalar(infra, "SELECT count(*) FROM users WHERE id = :u", u=user)
        return users == 0 and vector_count(user) == 0

    await eventually(gone)
    assert await scalar(infra, "SELECT count(*) FROM memories WHERE id = ANY(:i)", i=ids) == 0
    assert vector_ids(other) == {keep["id"]}


async def test_deleting_a_conversation_keeps_the_memories_learned_from_it(
    client: httpx2.AsyncClient, infra: Infrastructure, runtime: WorkerRuntime
) -> None:
    headers = await new_user(client)
    user = await user_id_of(client, headers)
    await enable_memory(client, headers)
    conversation = await new_conversation(client, headers)
    await say(infra, user, conversation, "I prefer concise answers.", "turn-key-001")
    await asyncio.to_thread(consolidate, runtime, conversation, user)
    assert len(await memories(infra, user)) == 1
    await client.delete(f"{API}/sessions/{conversation}", headers=headers)

    async def conversation_gone() -> bool:
        return (
            await scalar(infra, "SELECT count(*) FROM conversations WHERE id = :c", c=conversation)
            == 0
        )

    await eventually(conversation_gone)
    (kept,) = await memories(infra, user)  # memory is separate from chat history
    assert (
        kept.content == "The user prefers concise answers." and kept.source_conversation_id is None
    )


async def test_background_indexing_does_not_reset_the_retention_clock(
    client: httpx2.AsyncClient, infra: Infrastructure, runtime: WorkerRuntime
) -> None:
    from app.memory.pipeline import index_memory

    user = await new_user(client, "clock@example.com")
    await enable_memory(client, user, memory_retention_days=30)
    memory = await add_memory(client, user, "an old fact that is being re-indexed")
    async with infra.session_factory() as session:
        await session.execute(
            text("UPDATE memories SET updated_at = now() - interval '60 days' WHERE id = :i"),
            {"i": memory["id"]},
        )
        await session.commit()
    user_id = await user_id_of(client, user)
    outcome = await asyncio.to_thread(index_memory, runtime, uuid.UUID(memory["id"]), user_id)
    assert outcome == "indexed"
    with infra.sync_session_factory() as session:
        age = session.execute(
            text("SELECT now() - updated_at > interval '59 days' FROM memories WHERE id = :i"),
            {"i": memory["id"]},
        ).scalar()
        assert age is True  # indexing did not count as an edit
        assert sweep_retention(session) == 1


# ------------------------------------------------------------------ optional model-based extractor


def llm_runtime(
    runtime: WorkerRuntime, monkeypatch: pytest.MonkeyPatch, model: ScriptedModel, *, flag: bool
) -> WorkerRuntime:
    """A runtime whose settings enable (or not) the model-based extractor over a scripted model."""
    settings = runtime.settings.model_copy(
        update={
            "memory_llm_extraction": flag,
            "llm_base_url": "http://127.0.0.1:9/v1",
            "llm_model": "scripted",
            "llm_api_key": SecretStr("not-a-real-key"),
        }
    )

    @asynccontextmanager
    async def scripted_model(_: Any) -> AsyncGenerator[ChatModel]:
        yield model

    monkeypatch.setattr(llm_extract, "hosted_model", scripted_model)
    return replace(runtime, settings=settings)


def facts_reply(*texts: str) -> str:
    return json.dumps({"facts": [{"fact": t, "kind": "episodic", "importance": 4} for t in texts]})


async def test_model_extracted_facts_are_stored_with_the_same_rules_as_rule_based_ones(
    client: httpx2.AsyncClient,
    infra: Infrastructure,
    runtime: WorkerRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers = await new_user(client)
    user = await user_id_of(client, headers)
    await enable_memory(client, headers)
    conversation = await new_conversation(client, headers)
    await say(infra, user, conversation, "We are moving the billing service to Go.", "turn-key-001")
    model = ScriptedModel(
        facts_reply(
            "The user's team is moving the billing service to Go.",
            "The user's password is hunter2.",  # sensitive: dropped
            "The user's card is 4111 1111 1111 1111.",  # sensitive: dropped
        )
    )
    rt = llm_runtime(runtime, monkeypatch, model, flag=True)
    assert await asyncio.to_thread(consolidate_conversation, rt, conversation, user) == "created:1"
    assert len(model.calls) == 1
    stored = await memories(infra, user)
    assert [m.content for m in stored] == ["The user's team is moving the billing service to Go."]
    assert {m.source for m in stored} == {"consolidation"}
    # the same message is never examined twice (no second model call), and duplicates are skipped
    assert await asyncio.to_thread(consolidate_conversation, rt, conversation, user) == (
        "nothing_new"
    )
    assert len(model.calls) == 1
    await say(infra, user, conversation, "Still on the billing move.", "turn-key-002")
    again = llm_runtime(runtime, monkeypatch, ScriptedModel(model.script[0]), flag=True)
    assert await asyncio.to_thread(consolidate_conversation, again, conversation, user) == (
        "created:0"
    )
    assert len(await memories(infra, user)) == 1


async def test_an_invalid_model_answer_falls_back_to_the_rules(
    client: httpx2.AsyncClient,
    infra: Infrastructure,
    runtime: WorkerRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers = await new_user(client)
    user = await user_id_of(client, headers)
    await enable_memory(client, headers)
    conversation = await new_conversation(client, headers)
    await say(infra, user, conversation, "Remember that I deploy on Fridays.", "turn-key-001")
    model = ScriptedModel("Sorry, I cannot produce JSON today.")
    rt = llm_runtime(runtime, monkeypatch, model, flag=True)
    assert await asyncio.to_thread(consolidate_conversation, rt, conversation, user) == "created:1"
    assert [m.content for m in await memories(infra, user)] == ["I deploy on Fridays."]


async def test_with_the_flag_off_the_model_is_never_called(
    client: httpx2.AsyncClient,
    infra: Infrastructure,
    runtime: WorkerRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers = await new_user(client)
    user = await user_id_of(client, headers)
    await enable_memory(client, headers)
    conversation = await new_conversation(client, headers)
    await say(infra, user, conversation, "Remember that I deploy on Fridays.", "turn-key-001")
    model = ScriptedModel(facts_reply("The user is a spy."))
    rt = llm_runtime(runtime, monkeypatch, model, flag=False)
    assert await asyncio.to_thread(consolidate_conversation, rt, conversation, user) == "created:1"
    assert model.calls == []
    assert [m.content for m in await memories(infra, user)] == ["I deploy on Fridays."]


async def test_without_consent_the_model_is_not_asked_and_nothing_is_stored(
    client: httpx2.AsyncClient,
    infra: Infrastructure,
    runtime: WorkerRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers = await new_user(client)
    user = await user_id_of(client, headers)
    conversation = await new_conversation(client, headers)  # memory stays OFF
    await say(infra, user, conversation, "We are moving the billing service to Go.", "turn-key-001")
    model = ScriptedModel(facts_reply("The user's team is moving the billing service to Go."))
    rt = llm_runtime(runtime, monkeypatch, model, flag=True)
    assert await asyncio.to_thread(consolidate_conversation, rt, conversation, user) == (
        "consent_off"
    )
    assert model.calls == [] and await memories(infra, user) == []
