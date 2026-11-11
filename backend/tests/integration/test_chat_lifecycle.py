"""Deleting conversations and accounts removes messages, executions AND checkpoints (real worker).

The Celery worker runs in-process (solo pool) against real Redis and PostgreSQL, so the purge
travels the same path as in production: API tombstone -> outbox -> dispatcher -> task.
"""

from __future__ import annotations

import asyncio
import operator
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Annotated, Any

import httpx2
import pytest
from langgraph.graph import END, START, StateGraph  # pyright: ignore[reportMissingTypeStubs]
from sqlalchemy import text
from typing_extensions import TypedDict

from app.core.config import get_settings
from app.infra.container import Infrastructure
from app.ingestion.outbox import dispatch_outbox
from app.ingestion.reconcile import reconcile
from app.ingestion.runtime import WorkerRuntime
from app.services.chat_turns import ChatTurnService
from app.tasks.celery_app import celery_app
from tests.fakes import HashEmbedder
from tests.integration.docs_helpers import API, new_user, scalar

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _worker(runtime: WorkerRuntime, embedder: HashEmbedder) -> None:
    return None


class State(TypedDict):
    n: int
    log: Annotated[list[str], operator.add]


async def step(state: State) -> dict[str, Any]:
    return {"n": state.get("n", 0) + 1, "log": ["ran"]}


async def run_graph(infra: Infrastructure, thread: uuid.UUID) -> None:
    graph: Any = StateGraph(State)
    graph.add_node("step", step)
    graph.add_edge(START, "step")
    graph.add_edge("step", END)
    compiled = graph.compile(checkpointer=await infra.checkpointer.get())
    await compiled.ainvoke({"n": 0, "log": []}, {"configurable": {"thread_id": str(thread)}})


async def eventually(check: Callable[[], Awaitable[bool]], *, wait_s: float = 30.0) -> None:
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if await check():
            return
        await asyncio.sleep(0.2)
    raise AssertionError("condition not reached in time")


async def make_conversation_with_history(
    client: httpx2.AsyncClient, infra: Infrastructure, headers: dict[str, str]
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """A conversation with one finished turn and a real checkpoint. Returns (user, conv, thread)."""
    me = await client.get(f"{API}/auth/me", headers=headers)
    user = uuid.UUID(me.json()["id"])
    conversation = uuid.UUID(
        (await client.post(f"{API}/sessions", json={}, headers=headers)).json()["id"]
    )
    thread = await scalar(
        infra, "SELECT thread_id FROM conversations WHERE id = :id", id=conversation
    )
    async with infra.session_factory() as session:
        turns = ChatTurnService(session=session, settings=get_settings())
        handle = await turns.begin(
            user, conversation, idempotency_key="lifecycle-key-1", content="hello there"
        )
    async with infra.session_factory() as session:
        await ChatTurnService(session=session, settings=get_settings()).complete(
            user, handle.execution_id, answer="hi", response={"answer": "hi"}
        )
    await run_graph(infra, thread)
    return user, conversation, thread


async def checkpoint_rows(infra: Infrastructure, thread: uuid.UUID) -> int:
    total = 0
    for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
        total += int(
            await scalar(
                infra,
                f"SELECT count(*) FROM {table} WHERE thread_id = :t",  # noqa: S608
                t=str(thread),
            )
        )
    return total


async def test_history_and_checkpoints_survive_a_restart_of_the_api(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    headers = await new_user(client)
    _, conversation, thread = await make_conversation_with_history(client, infra, headers)
    restarted = Infrastructure.create(get_settings())
    try:
        async with restarted.session_factory() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT seq, role, content FROM messages "
                        "WHERE conversation_id = :c ORDER BY seq"
                    ),
                    {"c": conversation},
                )
            ).all()
        assert [(r.seq, r.role, r.content) for r in rows] == [
            (1, "user", "hello there"),
            (2, "assistant", "hi"),
        ]
        saver = await restarted.checkpointer.get()
        assert await saver.aget_tuple({"configurable": {"thread_id": str(thread)}}) is not None
    finally:
        await restarted.close()


async def test_deleting_a_session_purges_messages_executions_and_checkpoints(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    headers = await new_user(client)
    _, conversation, thread = await make_conversation_with_history(client, infra, headers)
    _, other, other_thread = await make_conversation_with_history(client, infra, headers)
    assert await checkpoint_rows(infra, thread) > 0

    resp = await client.delete(f"{API}/sessions/{conversation}", headers=headers)
    assert resp.status_code == 202

    async def purged() -> bool:
        return (
            await scalar(infra, "SELECT count(*) FROM conversations WHERE id = :c", c=conversation)
            == 0
        )

    await eventually(purged)
    for table in ("messages", "chat_executions"):
        count = await scalar(
            infra,
            f"SELECT count(*) FROM {table} WHERE conversation_id = :c",  # noqa: S608
            c=conversation,
        )
        assert count == 0, table
    assert await checkpoint_rows(infra, thread) == 0
    # the user's other conversation is untouched
    assert await checkpoint_rows(infra, other_thread) > 0
    assert (
        await scalar(infra, "SELECT count(*) FROM messages WHERE conversation_id = :c", c=other)
        == 2
    )


async def test_the_reconciler_republishes_a_purge_that_never_ran(
    client: httpx2.AsyncClient, infra: Infrastructure, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def no_kick(_infra: Infrastructure) -> None:
        return None

    monkeypatch.setattr("app.api.v1.routers.sessions.kick_dispatcher", no_kick)
    headers = await new_user(client)
    _, conversation, thread = await make_conversation_with_history(client, infra, headers)
    await client.delete(f"{API}/sessions/{conversation}", headers=headers)
    # simulate a lost message: the outbox row is gone before it was ever published
    async with infra.session_factory() as session:
        await session.execute(text("DELETE FROM outbox_events WHERE kind = 'purge_conversation'"))
        await session.commit()
    await asyncio.sleep(1.2)  # RECONCILE_PURGE_TTL_S=1 in the test environment
    with infra.sync_session_factory() as session:
        report = reconcile(session, get_settings())
    assert report.conversation_purges_requeued == 1
    dispatch_outbox(infra.sync_session_factory, celery_app, batch_size=50)

    async def purged() -> bool:
        return (
            await scalar(infra, "SELECT count(*) FROM conversations WHERE id = :c", c=conversation)
            == 0
        )

    await eventually(purged)
    assert await checkpoint_rows(infra, thread) == 0


async def test_deleting_the_account_removes_every_conversation_and_checkpoint(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    headers = await new_user(client)
    _, conv_a, thread_a = await make_conversation_with_history(client, infra, headers)
    _, conv_b, thread_b = await make_conversation_with_history(client, infra, headers)
    bystander = await new_user(client, "bystander@example.com")
    _, conv_keep, thread_keep = await make_conversation_with_history(client, infra, bystander)

    resp = await client.request(
        "DELETE",
        f"{API}/auth/account",
        json={"password": "correct-horse-battery-staple"},
        headers=headers,
    )
    assert resp.status_code == 202, resp.text

    async def purged() -> bool:
        return (
            await scalar(
                infra,
                "SELECT count(*) FROM conversations WHERE id IN (:a, :b)",
                a=conv_a,
                b=conv_b,
            )
            == 0
        )

    await eventually(purged)
    assert (
        await checkpoint_rows(infra, thread_a) == 0 and await checkpoint_rows(infra, thread_b) == 0
    )
    assert await scalar(infra, "SELECT count(*) FROM conversations WHERE id = :c", c=conv_keep) == 1
    assert await checkpoint_rows(infra, thread_keep) > 0  # another user's data is untouched
    dispatch_outbox(infra.sync_session_factory, celery_app, batch_size=50)


async def test_the_orphan_checkpoint_sweep_task_runs_in_the_worker(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    from app.tasks.chat import sweep_checkpoints_task

    orphan = uuid.uuid4()
    await run_graph(infra, orphan)
    assert await checkpoint_rows(infra, orphan) > 0
    removed = await asyncio.to_thread(sweep_checkpoints_task)
    assert removed > 0 and await checkpoint_rows(infra, orphan) == 0


def test_the_checkpoint_sweep_is_scheduled_by_beat() -> None:
    schedule = celery_app.conf.beat_schedule
    assert schedule["sweep-checkpoints"]["task"] == "chat.sweep_checkpoints"


async def cache_keys(infra: Infrastructure) -> list[str]:
    found: list[str] = []
    async for key in infra.redis.scan_iter(match="llmc:v1:u:*"):  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]
        found.append(key.decode() if isinstance(key, bytes) else str(key))  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    return found


async def test_deleting_the_account_removes_the_users_cached_answers(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    from app.llm.base import ChatMessage, LLMRequest
    from app.llm.cache import LLMGateway
    from tests.fakes import ScriptedModel

    headers = await new_user(client)
    me = await client.get(f"{API}/auth/me", headers=headers)
    user = uuid.UUID(me.json()["id"])
    other = uuid.uuid4()
    gateway = LLMGateway(ScriptedModel("cached"), infra.redis, get_settings())
    for owner, text_ in ((user, "mine"), (other, "someone else's")):
        request = LLMRequest(messages=(ChatMessage("user", text_),), user_id=owner)
        await gateway.remember(await gateway.complete(request))
    assert len(await cache_keys(infra)) == 2
    resp = await client.request(
        "DELETE",
        f"{API}/auth/account",
        json={"password": "correct-horse-battery-staple"},
        headers=headers,
    )
    assert resp.status_code == 202

    async def purged() -> bool:
        return (
            len(await cache_keys(infra)) == 1
            and await scalar(infra, "SELECT count(*) FROM users WHERE id = :u", u=user) == 0
        )

    await eventually(purged)
