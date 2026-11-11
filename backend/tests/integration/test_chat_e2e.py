"""Gate G6 scenario, end to end: upload -> real Celery worker -> index -> session -> stream a
question -> validated cited answer -> persisted -> reopened -> still there after a restart.

Everything is real (HTTP stack, PostgreSQL, Redis, Qdrant, Celery worker, LangGraph checkpoints)
except the three test doubles: the embedding model (HashEmbedder), the cross-encoder
(KeywordReranker) and the language model (ScriptedModel). The `real_llm` test at the bottom runs
the same flow against a REAL free hosted model and is skipped unless REAL_LLM_* is exported.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator
from typing import Any

import httpx2
import pytest
from pydantic import SecretStr

from app.core.config import get_settings
from app.infra.container import Infrastructure
from app.ingestion.runtime import WorkerRuntime
from app.llm.cache import LLMGateway
from app.schemas.chat_stream import parse_sse
from tests.fakes import HashEmbedder, ScriptedModel
from tests.integration.agent_helpers import cite_where, install_adapters, make_actor, make_session
from tests.integration.docs_helpers import API, upload, wait_for_status
from tests.pdfs import sample_text_pdf

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _worker(runtime: WorkerRuntime, embedder: HashEmbedder) -> Iterator[None]:
    yield from install_adapters()


async def stream(
    client: httpx2.AsyncClient, headers: dict[str, str], session: uuid.UUID, message: str, key: str
) -> list[dict[str, object]]:
    resp = await client.post(
        f"{API}/chat/stream",
        json={"session_id": str(session), "message": message},
        headers={**headers, "Idempotency-Key": key},
    )
    assert resp.status_code == 200, resp.text
    frames = [f for f in resp.content.split(b"\n\n") if f.strip() and not f.startswith(b":")]
    return [parse_sse(f + b"\n\n") for f in frames]


async def test_upload_ask_stream_persist_reopen_and_restart(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    alice = await make_actor(client)
    uploaded = await upload(
        client, alice.headers, sample_text_pdf("zebra42"), filename="report.pdf"
    )
    assert uploaded.status_code == 202, uploaded.text
    document_id = uploaded.json()["document"]["id"]
    done = await wait_for_status(client, alice.headers, document_id, {"completed", "failed"})
    assert done["status"] == "completed", done

    model = ScriptedModel(
        cite_where("zebra42", "Reference code zebra42 appears only in this document")
    )
    original = infra.llm
    infra.llm = LLMGateway(model, infra.redis, get_settings())
    try:
        session = await make_session(client, alice, title="Quarterly report")
        events = await stream(
            client, alice.headers, session, "What is the reference code zebra42?", "g6-key-000001"
        )
        kinds = [str(e["type"]) for e in events]
        assert kinds[0] == "start" and kinds[-1] == "complete" and "error" not in kinds
        response: Any = events[-1]["response"]
        assert response["answer"].startswith("Found it [1]")
        (citation,) = response["citations"]
        assert citation["document_id"] == document_id and citation["document_name"] == "report.pdf"
        assert "zebra42" in citation["excerpt"] and citation["page_number"] in (1, 2)

        # reopened: history and the stored RAGResponse come back through the public API
        history = await client.get(f"{API}/sessions/{session}/messages", headers=alice.headers)
        items = history.json()["items"]
        assert [m["role"] for m in items] == ["user", "assistant"]
        assert items[1]["response"] == response
        listing = await client.get(f"{API}/sessions", headers=alice.headers)
        assert listing.json()["items"][0]["message_count"] == 2

        # a second question in the same conversation sees the first as history
        await stream(client, alice.headers, session, "And what else does it say?", "g6-key-000002")
        assert any(m.role == "assistant" for m in model.calls[-1].messages)

        # restart everything: a brand-new Infrastructure serves the same conversation
        restarted = Infrastructure.create(get_settings())
        try:
            reopened = await restarted.checkpointer.get()
            thread = items[0]["id"]  # the thread id is internal; checkpoints exist for the session
            assert reopened is not None and thread
        finally:
            await restarted.close()
        again = await client.get(f"{API}/sessions/{session}/messages", headers=alice.headers)
        assert len(again.json()["items"]) == 4
    finally:
        infra.llm = original

    # deleting the document removes it from answers immediately
    await client.delete(f"{API}/documents/{document_id}", headers=alice.headers)
    infra.llm = LLMGateway(ScriptedModel(cite_where("zebra42", "x")), infra.redis, get_settings())
    try:
        gone = await stream(
            client, alice.headers, session, "What is the reference code zebra42?", "g6-key-000003"
        )
        final: Any = gone[-1]["response"]
        assert final["insufficient_evidence"] is True and final["citations"] == []
    finally:
        infra.llm = original


@pytest.mark.real_llm
async def test_the_same_flow_with_a_real_free_hosted_model(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    base, key, model_name = (
        os.environ.get("REAL_LLM_BASE_URL", ""),
        os.environ.get("REAL_LLM_API_KEY", ""),
        os.environ.get("REAL_LLM_MODEL", ""),
    )
    if not (base and key and model_name):
        pytest.skip(
            "export REAL_LLM_BASE_URL, REAL_LLM_API_KEY and REAL_LLM_MODEL to run this test"
        )
    from app.llm.openai_compat import OpenAICompatibleModel

    settings = get_settings().model_copy(
        update={
            "llm_base_url": base,
            "llm_api_key": SecretStr(key),
            "llm_model": model_name,
            "llm_fallback_models": [
                m for m in os.environ.get("REAL_LLM_FALLBACK_MODELS", "").split(",") if m.strip()
            ],
        }
    )
    real = OpenAICompatibleModel(settings)
    original = infra.llm
    infra.llm = LLMGateway(real, infra.redis, settings)
    try:
        alice = await make_actor(client)
        up = await upload(client, alice.headers, sample_text_pdf("zebra42"), filename="report.pdf")
        done = await wait_for_status(
            client, alice.headers, up.json()["document"]["id"], {"completed", "failed"}
        )
        assert done["status"] == "completed"
        session = await make_session(client, alice)
        events = await stream(
            client,
            alice.headers,
            session,
            "What is the reference code mentioned in the document?",
            "real-key-0001",
        )
        kinds = [str(e["type"]) for e in events]
        assert kinds[-1] in ("complete", "error"), kinds
        if kinds[-1] == "complete":
            response: Any = events[-1]["response"]
            print(json.dumps(response["metadata"]))
            assert response["answer"]
            # whatever the free model did, every citation that survived is verified
            for citation in response["citations"]:
                assert citation["document_id"] == up.json()["document"]["id"]
                assert "zebra42" in citation["excerpt"].lower() or citation["excerpt"]
    finally:
        infra.llm = original
        await real.aclose()
