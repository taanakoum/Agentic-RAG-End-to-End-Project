"""Upload -> real Celery worker -> index -> search, through the public HTTP API.

The worker and the API share the deterministic test adapters; one `real_model` test uses the real
BAAI/bge-small-en-v1.5, Qdrant/bm25 and cross-encoder models end to end.
"""

from __future__ import annotations

from typing import Any

import httpx2
import pytest

from app.ingestion.embedding import FastEmbedEmbedder, set_embedder_factory
from app.ingestion.runtime import WorkerRuntime
from app.retrieval.rerank import FastEmbedReranker, set_reranker_factory
from tests.fakes import HashEmbedder, KeywordReranker
from tests.integration.docs_helpers import API, new_user, upload, wait_for_status
from tests.pdfs import sample_text_pdf, sectioned_pdf

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _worker(runtime: WorkerRuntime, embedder: HashEmbedder) -> Any:
    set_reranker_factory(KeywordReranker)
    yield
    set_reranker_factory(None)


async def upload_completed(
    client: httpx2.AsyncClient, headers: dict[str, str], data: bytes, name: str
) -> dict[str, Any]:
    resp = await upload(client, headers, data, filename=name)
    assert resp.status_code == 202, resp.text
    doc = await wait_for_status(
        client, headers, resp.json()["document"]["id"], {"completed", "failed"}
    )
    assert doc["status"] == "completed", doc
    return doc


async def test_an_uploaded_document_becomes_searchable_with_correct_citation_fields(
    client: httpx2.AsyncClient,
) -> None:
    headers = await new_user(client)
    doc = await upload_completed(client, headers, sample_text_pdf("zebra42"), "report.pdf")
    resp = await client.post(
        f"{API}/search", json={"query": "Reference code zebra42"}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    results = resp.json()["results"]
    assert results, "the freshly ingested document must be retrievable"
    best = results[0]
    assert best["document_id"] == doc["id"] and best["document_name"] == "report.pdf"
    # the reference code is on page 2; chunks may start earlier, but must reach page 2
    assert "zebra42" in best["text"] and best["page_number"] <= 2 <= best["page_end"]
    assert best["scores"]["dense_rank"] is not None and best["scores"]["sparse_rank"] is not None


async def test_a_document_stops_being_searchable_as_soon_as_it_is_deleted(
    client: httpx2.AsyncClient,
) -> None:
    headers = await new_user(client)
    doc = await upload_completed(client, headers, sample_text_pdf("yak77"), "report.pdf")
    query = {"query": "Reference code yak77"}
    assert (await client.post(f"{API}/search", json=query, headers=headers)).json()["results"]
    deleted = await client.delete(f"{API}/documents/{doc['id']}", headers=headers)
    assert deleted.status_code == 202
    # tombstoned documents are excluded immediately, even before the purge has removed vectors
    after = await client.post(f"{API}/search", json=query, headers=headers)
    assert after.status_code == 200 and after.json()["results"] == []


async def test_two_users_uploading_the_same_text_only_find_their_own_copy(
    client: httpx2.AsyncClient,
) -> None:
    ha = await new_user(client, "alice@example.com")
    hb = await new_user(client, "bob@example.com")
    a = await upload_completed(client, ha, sectioned_pdf("shared1"), "mine.pdf")
    b = await upload_completed(client, hb, sectioned_pdf("shared1"), "theirs.pdf")
    assert a["id"] != b["id"]
    for headers, own in ((ha, a["id"]), (hb, b["id"])):
        resp = await client.post(
            f"{API}/search", json={"query": "financial results", "top_k": 20}, headers=headers
        )
        ids = {r["document_id"] for r in resp.json()["results"]}
        assert ids == {own}


@pytest.mark.real_model
async def test_search_with_the_real_embedding_and_cross_encoder_models(
    client: httpx2.AsyncClient, runtime: WorkerRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    embedder = FastEmbedEmbedder(runtime.settings)
    monkeypatch.setattr(runtime, "embedder", embedder)
    set_embedder_factory(lambda _settings: embedder)  # the API process side
    set_reranker_factory(FastEmbedReranker)
    headers = await new_user(client)
    await upload_completed(client, headers, sectioned_pdf("real1"), "financials.pdf")
    resp = await client.post(
        f"{API}/search",
        json={"query": "Which page discusses the financial results?"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["degraded"] == [], body["degraded"]
    top = body["results"][0]
    assert top["scores"]["rerank_score"] is not None
    assert "financial" in (top["text"] + top["section_path"]).lower()
    assert top["page_number"] == 2
