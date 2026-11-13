"""Adaptive re-rank skip through the real Retriever (real Qdrant; KeywordReranker counts calls)."""

from __future__ import annotations

from collections.abc import Iterator

import httpx2
import pytest

from app.core.config import Settings, get_settings
from app.infra.container import Infrastructure
from app.ingestion.embedding import set_embedder_factory
from app.retrieval.models import RetrievalMode, RetrievalResult
from app.retrieval.rerank import set_reranker_factory
from app.retrieval.retriever import Retriever
from tests.fakes import HashEmbedder, KeywordReranker
from tests.integration.docs_helpers import new_user
from tests.integration.retrieval_helpers import seed_document
from tests.integration.test_retrieval import me

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _adapters() -> Iterator[None]:
    set_embedder_factory(HashEmbedder)
    set_reranker_factory(KeywordReranker)
    yield
    set_embedder_factory(None)
    set_reranker_factory(None)


def make(infra: Infrastructure, settings: Settings, reranker: KeywordReranker) -> Retriever:
    return Retriever(
        infra.qdrant, settings, HashEmbedder(settings), reranker, infra.inference_executor
    )


async def run(
    retriever: Retriever, user: object, query: str, docs: list[object]
) -> RetrievalResult:
    return await retriever.search(
        user_id=user,  # pyright: ignore[reportArgumentType]
        query=query,
        document_ids=docs,  # pyright: ignore[reportArgumentType]
        mode=RetrievalMode.HYBRID_RERANK,
        top_k=5,
    )


async def test_a_confident_query_does_not_call_the_reranker_and_an_ambiguous_one_does(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    headers = await new_user(client)
    uid = await me(client, headers)
    # "alpha bravo" is in ONE chunk (rank 1 in both rankers, the rest only in the dense list);
    # "kilo" is in two chunks that tie for the lead.
    doc = await seed_document(
        infra,
        uid,
        "d.pdf",
        [
            "alpha bravo charlie",
            "delta echo foxtrot",
            "golf hotel india",
            "kilo lima one",
            "kilo lima two",
        ],
    )
    settings = get_settings().model_copy(update={"rerank_skip_margin": 0.3})
    reranker = KeywordReranker()
    r = make(infra, settings, reranker)

    confident = await run(r, uid, "alpha bravo", [doc])
    assert reranker.calls == 0 and confident.rerank_skipped is True
    assert "rerank" not in confident.timings_ms
    assert confident.chunks[0].hit.text == "alpha bravo charlie"
    assert all(c.rerank_score is None for c in confident.chunks)
    assert len(confident.chunks) == 5  # the skip never shortens the answer

    ambiguous = await run(r, uid, "kilo lima", [doc])
    assert reranker.calls == 1 and ambiguous.rerank_skipped is False
    assert any(c.rerank_score is not None for c in ambiguous.chunks)
    assert "rerank" in ambiguous.timings_ms


async def test_the_skip_is_off_by_default_and_with_margin_zero(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    headers = await new_user(client)
    uid = await me(client, headers)
    doc = await seed_document(
        infra, uid, "d.pdf", ["alpha bravo charlie", "delta echo foxtrot", "golf hotel india"]
    )
    off = get_settings().model_copy(update={"rerank_skip_margin": 0.0})
    reranker = KeywordReranker()
    result = await run(make(infra, off, reranker), uid, "alpha bravo", [doc])
    assert reranker.calls == 1 and result.rerank_skipped is False


async def test_skipping_never_widens_authorization(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    alice = await new_user(client, "alice@example.com")
    bob = await new_user(client, "bob@example.com")
    alice_id, bob_id = await me(client, alice), await me(client, bob)
    texts = ["alpha bravo charlie", "delta echo foxtrot"]
    alice_doc = await seed_document(infra, alice_id, "a.pdf", texts)
    await seed_document(infra, bob_id, "b.pdf", texts)  # same words, another owner
    settings = get_settings().model_copy(update={"rerank_skip_margin": 0.3})
    reranker = KeywordReranker()
    result = await run(make(infra, settings, reranker), alice_id, "alpha bravo", [alice_doc])
    assert result.rerank_skipped is True
    assert {c.hit.document_id for c in result.chunks} == {alice_doc}
    # asking with the OTHER user's document id finds nothing, skip or not
    other = await seed_document(infra, bob_id, "c.pdf", texts)
    leaked = await run(make(infra, settings, reranker), alice_id, "alpha bravo", [other])
    assert leaked.chunks == []
