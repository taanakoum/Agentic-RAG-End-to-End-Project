"""The PostgreSQL LangGraph checkpointer with a real (LLM-free) graph, restarts and deletion."""

from __future__ import annotations

import asyncio
import operator
import uuid
from typing import Annotated, Any

import httpx2
import pytest
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, START, StateGraph  # pyright: ignore[reportMissingTypeStubs]
from sqlalchemy import text
from typing_extensions import TypedDict

from app.chat.checkpointer import CheckpointerProvider
from app.chat.purge import delete_checkpoint_threads, sweep_orphan_checkpoints
from app.core.config import get_settings
from app.infra.container import Infrastructure
from tests.integration.docs_helpers import API, new_user

pytestmark = pytest.mark.integration


class State(TypedDict):
    n: int
    log: Annotated[list[str], operator.add]


async def step(state: State) -> dict[str, Any]:
    n = state.get("n", 0) + 1
    return {"n": n, "log": [f"step{n}"]}


def build_graph(saver: AsyncPostgresSaver) -> Any:
    graph: Any = StateGraph(State)  # langgraph.graph ships without type information
    graph.add_node("step", step)
    graph.add_edge(START, "step")
    graph.add_edge("step", END)
    return graph.compile(checkpointer=saver)


def config(thread: uuid.UUID) -> Any:
    return {"configurable": {"thread_id": str(thread)}}


async def thread_rows(infra: Infrastructure, thread: uuid.UUID) -> int:
    total = 0
    async with infra.session_factory() as session:
        for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
            total += (
                await session.execute(
                    text(f"SELECT count(*) FROM {table} WHERE thread_id = :t"),  # noqa: S608
                    {"t": str(thread)},
                )
            ).scalar_one()
    return int(total)


async def test_state_survives_a_full_restart_and_continues(infra: Infrastructure) -> None:
    thread = uuid.uuid4()
    saver = await infra.checkpointer.get()
    first = await build_graph(saver).ainvoke({"n": 0, "log": ["start"]}, config(thread))
    assert first["n"] == 1 and first["log"] == ["start", "step1"]

    # "restart everything": a brand-new process-level infrastructure with a new connection pool
    restarted = Infrastructure.create(get_settings())
    try:
        saver2 = await restarted.checkpointer.get()
        assert saver2 is not saver
        graph = build_graph(saver2)
        stored = await graph.aget_state(config(thread))
        assert stored.values == {"n": 1, "log": ["start", "step1"]}
        second = await graph.ainvoke({"log": ["again"]}, config(thread))  # continues the thread
        assert second["n"] == 2 and second["log"] == ["start", "step1", "again", "step2"]
    finally:
        await restarted.close()
    # and the first process still sees the newest checkpoint
    latest = await build_graph(saver).aget_state(config(thread))
    assert latest.values["n"] == 2


async def test_threads_are_independent(infra: Infrastructure) -> None:
    saver = await infra.checkpointer.get()
    graph = build_graph(saver)
    a, b = uuid.uuid4(), uuid.uuid4()
    await graph.ainvoke({"n": 10, "log": ["a"]}, config(a))
    assert (await graph.aget_state(config(b))).values == {}
    await graph.ainvoke({"n": 20, "log": ["b"]}, config(b))
    assert (await graph.aget_state(config(a))).values["n"] == 11
    assert (await graph.aget_state(config(b))).values["n"] == 21


async def test_many_threads_run_concurrently_without_interference(infra: Infrastructure) -> None:
    saver = await infra.checkpointer.get()
    graph = build_graph(saver)
    threads = [uuid.uuid4() for _ in range(25)]
    results = await asyncio.gather(
        *(graph.ainvoke({"n": i, "log": [f"t{i}"]}, config(t)) for i, t in enumerate(threads))
    )
    assert [r["n"] for r in results] == [i + 1 for i in range(25)]
    for i, t in enumerate(threads):
        assert (await graph.aget_state(config(t))).values["log"] == [f"t{i}", f"step{i + 1}"]


async def test_processes_starting_together_can_all_set_up_the_tables() -> None:
    providers = [CheckpointerProvider(get_settings()) for _ in range(4)]
    try:
        savers = await asyncio.gather(*(p.get() for p in providers))
        assert len({id(s) for s in savers}) == 4
    finally:
        await asyncio.gather(*(p.close() for p in providers))


async def test_the_provider_returns_one_saver_per_process_even_when_called_concurrently() -> None:
    provider = CheckpointerProvider(get_settings())
    try:
        savers = await asyncio.gather(*(provider.get() for _ in range(10)))
        assert len({id(s) for s in savers}) == 1
    finally:
        await provider.close()
    assert provider._saver is None  # pyright: ignore[reportPrivateUsage]
    await provider.close()  # closing twice is harmless


async def test_deleting_a_thread_removes_exactly_its_checkpoints(infra: Infrastructure) -> None:
    saver = await infra.checkpointer.get()
    graph = build_graph(saver)
    doomed, keep = uuid.uuid4(), uuid.uuid4()
    await graph.ainvoke({"n": 0, "log": []}, config(doomed))
    await graph.ainvoke({"n": 0, "log": []}, config(doomed))  # several checkpoints
    await graph.ainvoke({"n": 0, "log": []}, config(keep))
    assert await thread_rows(infra, doomed) > 2 and await thread_rows(infra, keep) > 0
    with infra.sync_session_factory() as session:
        removed = delete_checkpoint_threads(session, [doomed])
        session.commit()
    assert removed > 2
    assert await thread_rows(infra, doomed) == 0
    # the library itself no longer sees the thread, and still sees the other one
    assert await saver.aget_tuple(config(doomed)) is None
    assert (await graph.aget_state(config(doomed))).values == {}
    assert (await graph.aget_state(config(keep))).values["n"] == 1


async def test_orphan_sweep_removes_checkpoints_without_a_conversation_and_keeps_live_ones(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    saver = await infra.checkpointer.get()
    graph = build_graph(saver)
    headers = await new_user(client)
    created = await client.post(f"{API}/sessions", json={}, headers=headers)
    async with infra.session_factory() as session:
        live_thread = (
            await session.execute(
                text("SELECT thread_id FROM conversations WHERE id = :id"),
                {"id": created.json()["id"]},
            )
        ).scalar_one()
    orphan = uuid.uuid4()
    await graph.ainvoke({"n": 0, "log": []}, config(live_thread))
    await graph.ainvoke({"n": 0, "log": []}, config(orphan))
    with infra.sync_session_factory() as session:
        assert sweep_orphan_checkpoints(session) > 0
    assert await thread_rows(infra, orphan) == 0
    assert await thread_rows(infra, live_thread) > 0
    with infra.sync_session_factory() as session:
        assert sweep_orphan_checkpoints(session) == 0  # idempotent


async def test_deleting_without_any_checkpoint_tables_or_ids_is_a_no_op(
    infra: Infrastructure,
) -> None:
    await infra.checkpointer.get()
    with infra.sync_session_factory() as session:
        assert delete_checkpoint_threads(session, []) == 0
        assert delete_checkpoint_threads(session, [uuid.uuid4()]) == 0
