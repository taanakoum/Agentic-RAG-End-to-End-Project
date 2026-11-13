"""Traces and metrics across the real stack: API -> graph -> retrieval -> model, and
API -> outbox -> Celery worker. Also the privacy rule: no user content ever reaches a span."""

from __future__ import annotations

from collections.abc import Iterator

import httpx2
import pytest
from opentelemetry.trace import SpanKind

from app.infra.container import Infrastructure
from app.ingestion.runtime import WorkerRuntime
from tests import telemetry_helpers as th
from tests.fakes import HashEmbedder, ScriptedModel
from tests.integration.agent_helpers import (
    install_adapters,
    make_actor,
    make_session,
    scripted,
    seed_handbook,
)
from tests.integration.docs_helpers import new_user, upload, wait_for_status
from tests.integration.test_chat_api import QUESTION, post
from tests.pdfs import blank_pdf, sectioned_pdf

pytestmark = pytest.mark.integration

__all__ = ["scripted"]


@pytest.fixture(autouse=True)
def _adapters() -> Iterator[None]:
    yield from install_adapters()


# ------------------------------------------------------------------ one chat turn


async def test_a_chat_turn_is_one_trace_from_the_http_request_down_to_the_model_call(
    client: httpx2.AsyncClient, infra: Infrastructure, scripted: ScriptedModel
) -> None:
    alice = await make_actor(client)
    await seed_handbook(infra, alice)
    session = await make_session(client, alice)
    th.EXPORTER.clear()

    response = await post(client, alice, session)
    assert response.status_code == 200, response.text

    (turn,) = th.named("chat.turn")
    trace = th.trace_id_of(turn)
    in_trace = {s.name for s in th.spans() if th.trace_id_of(s) == trace}
    expected = {
        "chat.turn",
        "agent.classify",
        "agent.prepare_query",
        "agent.retrieve",
        "agent.generate",
        "agent.check_output",
        "agent.finalize",
        "retrieval.search",
        "llm.call",
    }
    assert expected <= in_trace, expected - in_trace
    # the cross-encoder either ran (its own span) or was skipped because fusion was confident,
    # which the search span must then say (ADR-038)
    if "retrieval.rerank" not in in_trace:
        (search,) = [s for s in th.named("retrieval.search") if th.trace_id_of(s) == trace]
        assert (
            search.attributes is not None and search.attributes["retrieval.rerank_skipped"] is True
        )
    assert any(name.startswith("POST") for name in in_trace)  # the FastAPI server span
    assert response.headers["x-trace-id"] == trace  # a user can quote it in a bug report

    # the request id (log correlation) is on the server span of the same trace
    server = next(s for s in th.spans() if s.kind is SpanKind.SERVER and th.trace_id_of(s) == trace)
    assert server.attributes is not None
    assert server.attributes["app.request_id"] == response.headers["x-request-id"]

    # hierarchy: the model call is below the generate node, which is below the turn
    by_id = {s.context.span_id: s for s in th.spans() if s.context is not None}

    def ancestors(name: str) -> list[str]:
        chain: list[str] = []
        current = th.named(name)[0]
        while current.parent is not None and current.parent.span_id in by_id:
            current = by_id[current.parent.span_id]
            chain.append(current.name)
        return chain

    assert "agent.generate" in ancestors("llm.call") and "chat.turn" in ancestors("llm.call")
    assert "agent.retrieve" in ancestors("retrieval.search")

    (call,) = th.named("llm.call")
    assert call.attributes is not None
    assert call.attributes["llm.cache"] == "miss" and call.attributes["llm.model"]
    assert th.metric_points("llm.requests")
    assert th.metric_points("chat.turns") and th.metric_points("chat.turn.duration")
    assert th.metric_points("retrieval.duration")


async def test_a_cache_hit_is_visible_in_the_trace_and_spends_no_model_call(
    client: httpx2.AsyncClient, infra: Infrastructure, scripted: ScriptedModel
) -> None:
    alice = await make_actor(client)
    await seed_handbook(infra, alice)
    await post(client, alice, await make_session(client, alice))
    th.EXPORTER.clear()
    await post(client, alice, await make_session(client, alice))
    (call,) = th.named("llm.call")
    assert call.attributes is not None and call.attributes["llm.cache"] == "hit"
    assert len(scripted.calls) == 1


async def test_user_content_never_reaches_a_span_or_an_event(
    client: httpx2.AsyncClient, infra: Infrastructure, scripted: ScriptedModel
) -> None:
    alice = await make_actor(client, "dana.private@example.com")
    await seed_handbook(infra, alice, "confidential-handbook.pdf")
    session = await make_session(client, alice)
    th.EXPORTER.clear()
    response = await post(client, alice, session, QUESTION)
    body = response.json()["response"]
    assert response.status_code == 200 and body["citations"]

    dump = th.serialized()
    token = alice.headers["Authorization"].split()[-1]
    forbidden = [
        QUESTION,
        "maximum upload size",
        body["answer"],
        body["citations"][0]["excerpt"],
        "25 megabytes",
        "confidential-handbook",
        "dana.private",
        "example.com",
        token,
        str(alice.id),  # only the hashed pseudonym may appear
    ]
    for needle in forbidden:
        assert needle not in dump, needle
    assert th.named("db SELECT")  # database spans exist, and carry no statement text
    for fragment in (
        " FROM ",
        "INSERT INTO",
        "UPDATE ",
        " WHERE ",
        " VALUES",
        "users",
        "conversations",
    ):
        assert fragment not in dump, fragment
    server = next(s for s in th.spans() if s.kind is SpanKind.SERVER)
    assert server.attributes is not None
    assert "Authorization" not in dump and "Bearer" not in dump
    assert not any(key.startswith("http.request.header") for key in server.attributes)


async def test_a_failing_model_marks_the_span_with_the_error_type_only(
    client: httpx2.AsyncClient, infra: Infrastructure, scripted: ScriptedModel
) -> None:
    from app.core.exceptions import ModelProviderError

    alice = await make_actor(client)
    await seed_handbook(infra, alice)
    scripted.script = [ModelProviderError("provider said: the secret prompt text leaked")]
    th.EXPORTER.clear()
    response = await post(client, alice, await make_session(client, alice))
    assert response.status_code >= 400 or response.json()["response"]["insufficient_evidence"]
    failed = [s for s in th.named("llm.call") if s.attributes and "error.type" in s.attributes]
    assert failed and failed[0].attributes is not None
    assert failed[0].attributes["error.type"] == "ModelProviderError"
    assert "secret prompt text" not in th.serialized()


async def test_database_statements_are_spans_of_the_same_trace_without_statement_text(
    client: httpx2.AsyncClient, infra: Infrastructure, scripted: ScriptedModel
) -> None:
    alice = await make_actor(client)
    await seed_handbook(infra, alice)
    session = await make_session(client, alice)
    th.EXPORTER.clear()
    response = await post(client, alice, session)
    assert response.status_code == 200, response.text

    trace = response.headers["x-trace-id"]
    in_trace = [s for s in th.spans() if th.trace_id_of(s) == trace and s.name.startswith("db ")]
    names = {s.name for s in in_trace}
    assert "db SELECT" in names and "db INSERT" in names, names
    for item in in_trace:
        assert item.kind is SpanKind.CLIENT and item.attributes is not None
        assert item.attributes["db.system"] == "postgresql"
        assert item.attributes["db.operation"] == item.name.removeprefix("db ")
        assert not any(key in item.attributes for key in ("db.statement", "db.query.text"))
        assert all(" FROM " not in str(value) for value in item.attributes.values())
    # a database span hangs below the request or a node, never loose in another trace
    server = next(s for s in th.spans() if s.kind is SpanKind.SERVER and th.trace_id_of(s) == trace)
    assert server.context is not None
    assert any(s.parent is not None for s in in_trace)


async def test_a_failing_statement_ends_its_span_with_the_error_type_only(
    infra: Infrastructure,
) -> None:
    from sqlalchemy import text
    from sqlalchemy.exc import ProgrammingError

    from app.core.telemetry import span

    th.EXPORTER.clear()
    with span("test.root"):
        async with infra.engine.connect() as conn:
            with pytest.raises(ProgrammingError):
                await conn.execute(text("SELECT secret_column_name FROM no_such_table"))
    failed = [s for s in th.named("db SELECT") if s.attributes and "error.type" in s.attributes]
    assert failed and failed[0].attributes is not None
    assert failed[0].attributes["error.type"] in {"UndefinedTable", "ProgrammingError"}
    assert "no_such_table" not in th.serialized() and "secret_column_name" not in th.serialized()
    assert failed[0].status.status_code.name == "ERROR"


async def test_statements_outside_any_trace_are_not_recorded(infra: Infrastructure) -> None:
    from sqlalchemy import text

    th.EXPORTER.clear()
    async with infra.engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    assert not [s for s in th.spans() if s.name.startswith("db ")]


# ------------------------------------------------------------------ upload -> outbox -> worker


async def test_ingestion_in_the_worker_continues_the_trace_of_the_upload_request(
    client: httpx2.AsyncClient, runtime: WorkerRuntime, embedder: HashEmbedder
) -> None:
    headers = await new_user(client)
    th.EXPORTER.clear()
    up = await upload(client, headers, sectioned_pdf("trace"), filename="private-report.pdf")
    assert up.status_code in {200, 202}, up.text
    trace = up.headers["x-trace-id"]
    doc = await wait_for_status(client, headers, up.json()["document"]["id"], {"completed"})
    assert doc["status"] == "completed"

    task = [s for s in th.named("task.process_document") if th.trace_id_of(s) == trace]
    assert len(task) == 1, "the worker span must continue the upload request's trace"
    stages = {s.name for s in th.spans() if th.trace_id_of(s) == trace}
    assert {"ingestion.parse", "ingestion.embed", "ingestion.index"} <= stages
    assert task[0].attributes is not None and task[0].attributes["job.outcome"]
    parse = th.named("ingestion.parse")[0]
    assert parse.attributes is not None and int(str(parse.attributes["ingestion.chunks"])) >= 2
    assert th.metric_points("ingestion.stage.duration")
    assert "private-report" not in th.serialized()  # the file name is user content
    db = [s for s in th.spans() if th.trace_id_of(s) == trace and s.name.startswith("db ")]
    assert {"db SELECT", "db INSERT"} <= {s.name for s in db}  # request and worker, one trace
    assert any(s.name == "db UPDATE" for s in db)


async def test_a_permanently_failing_document_is_a_traceable_failure(
    client: httpx2.AsyncClient, runtime: WorkerRuntime, embedder: HashEmbedder
) -> None:
    headers = await new_user(client)
    th.EXPORTER.clear()
    up = await upload(client, headers, blank_pdf())
    trace = up.headers["x-trace-id"]
    doc = await wait_for_status(client, headers, up.json()["document"]["id"], {"failed"})
    assert doc["error"]["code"] == "DOCUMENT_NO_TEXT"
    task = [s for s in th.named("task.process_document") if th.trace_id_of(s) == trace]
    assert task, "failed jobs must still be reachable from the request trace"
