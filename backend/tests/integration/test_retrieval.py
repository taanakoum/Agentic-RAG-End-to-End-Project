"""Hybrid retrieval against real PostgreSQL + Qdrant (HashEmbedder and KeywordReranker are the
only test adapters). Covers fusion, reranking, graceful degradation and, above all, authorization.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Iterator
from typing import Any

import httpx2
import pytest
from sqlalchemy import text as sa_text

from app.core.config import Settings, get_settings
from app.core.exceptions import ExternalServiceUnavailableError, RetrievalTimeoutError
from app.infra.container import Infrastructure
from app.ingestion.embedding import SparseVec, set_embedder_factory
from app.retrieval.models import RetrievalMode
from app.retrieval.rerank import set_reranker_factory
from app.retrieval.retriever import Retriever
from tests.fakes import HashEmbedder, KeywordReranker
from tests.integration.docs_helpers import API, bearer, new_user
from tests.integration.retrieval_helpers import seed_document

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _adapters() -> Iterator[None]:
    set_embedder_factory(HashEmbedder)
    set_reranker_factory(KeywordReranker)
    yield
    set_embedder_factory(None)
    set_reranker_factory(None)


async def me(client: httpx2.AsyncClient, headers: dict[str, str]) -> uuid.UUID:
    resp = await client.get(f"{API}/auth/me", headers=headers)
    assert resp.status_code == 200, resp.text
    return uuid.UUID(resp.json()["id"])


def retriever(
    infra: Infrastructure,
    *,
    settings: Settings | None = None,
    embedder: HashEmbedder | None = None,
    reranker: KeywordReranker | None = None,
) -> Retriever:
    s = settings or get_settings()
    return Retriever(
        infra.qdrant,
        s,
        embedder or HashEmbedder(s),
        reranker or KeywordReranker(),
        infra.inference_executor,
    )


async def search(
    client: httpx2.AsyncClient, headers: dict[str, str], query: str, **body: Any
) -> httpx2.Response:
    return await client.post(f"{API}/search", json={"query": query, **body}, headers=headers)


# ------------------------------------------------------------------ fusion and rankers


async def test_chunks_found_by_only_one_ranker_still_reach_the_fused_set(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    headers = await new_user(client)
    uid = await me(client, headers)
    settings = get_settings().model_copy(
        update={"retrieval_dense_limit": 1, "retrieval_sparse_limit": 1}
    )
    emb = HashEmbedder(settings)
    query = "alpha bravo"
    q_dense = emb.embed_dense([query])[0]
    # chunk A: identical to the query in dense space, no shared words (sparse cannot see it)
    # chunk B: shares the words (sparse finds it) but its dense vector is orthogonal to the query
    orthogonal = [0.0] * settings.embedding_dim
    used = {i for i, v in enumerate(q_dense) if v}
    orthogonal[next(i for i in range(settings.embedding_dim) if i not in used)] = 1.0
    doc = await seed_document(
        infra,
        uid,
        "mix.pdf",
        ["wholly unrelated filler text", "alpha bravo charlie"],
        dense=[q_dense, orthogonal],
    )
    result = await retriever(infra, settings=settings).search(
        user_id=uid, query=query, document_ids=[doc], mode=RetrievalMode.HYBRID, top_k=10
    )
    by_text = {c.hit.text: c for c in result.chunks}
    a, b = by_text["wholly unrelated filler text"], by_text["alpha bravo charlie"]
    assert (a.dense_rank, a.sparse_rank) == (1, None)
    assert (b.dense_rank, b.sparse_rank) == (None, 1)
    assert result.candidates == {"dense": 1, "sparse": 1, "fused": 2}
    k = settings.rrf_k
    assert a.fused_score == pytest.approx(1 / (k + 1))
    assert b.fused_score == pytest.approx(1 / (k + 1))


async def test_a_chunk_found_by_both_rankers_ranks_above_chunks_found_by_one(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    headers = await new_user(client)
    uid = await me(client, headers)
    doc = await seed_document(
        infra,
        uid,
        "d.pdf",
        ["alpha bravo charlie", "alpha delta echo", "foxtrot golf hotel"],
    )
    result = await retriever(infra).search(
        user_id=uid,
        query="alpha bravo charlie",
        document_ids=[doc],
        mode=RetrievalMode.HYBRID,
        top_k=10,
    )
    top = result.chunks[0]
    assert top.hit.text == "alpha bravo charlie"
    assert top.dense_rank == 1 and top.sparse_rank == 1
    assert top.fused_score == pytest.approx(2 / (get_settings().rrf_k + 1))


async def test_every_mode_returns_results_and_reports_its_own_rankers(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    headers = await new_user(client)
    uid = await me(client, headers)
    doc = await seed_document(infra, uid, "d.pdf", ["alpha one", "alpha two", "beta three"])
    r = retriever(infra)
    dense = await r.search(
        user_id=uid, query="alpha", document_ids=[doc], mode=RetrievalMode.DENSE, top_k=5
    )
    sparse = await r.search(
        user_id=uid, query="alpha", document_ids=[doc], mode=RetrievalMode.SPARSE, top_k=5
    )
    hybrid = await r.search(
        user_id=uid, query="alpha", document_ids=[doc], mode=RetrievalMode.HYBRID, top_k=5
    )
    rerank = await r.search(
        user_id=uid, query="alpha", document_ids=[doc], mode=RetrievalMode.HYBRID_RERANK, top_k=5
    )
    assert all(c.sparse_rank is None for c in dense.chunks) and dense.chunks
    assert all(c.dense_rank is None for c in sparse.chunks) and sparse.chunks
    assert {c.hit.text for c in sparse.chunks} == {"alpha one", "alpha two"}
    assert all(c.rerank_score is None for c in hybrid.chunks)
    assert all(c.rerank_score is not None for c in rerank.chunks)


async def test_top_k_limits_the_number_of_returned_chunks(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    headers = await new_user(client)
    uid = await me(client, headers)
    doc = await seed_document(infra, uid, "d.pdf", [f"alpha item {i}" for i in range(12)])
    result = await retriever(infra).search(
        user_id=uid, query="alpha", document_ids=[doc], mode=RetrievalMode.HYBRID, top_k=3
    )
    assert len(result.chunks) == 3


# ------------------------------------------------------------------ reranking


async def test_rerank_promotes_the_chunk_the_cross_encoder_prefers(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    headers = await new_user(client)
    uid = await me(client, headers)
    texts = ["alpha alpha alpha", "alpha alpha beta", "alpha beta gamma", "alpha zulu"]
    doc = await seed_document(infra, uid, "d.pdf", texts)
    ranker = KeywordReranker()
    ranker.prefer = "zulu"
    r = retriever(infra, reranker=ranker)
    kwargs: dict[str, Any] = {"user_id": uid, "query": "alpha", "document_ids": [doc], "top_k": 4}
    hybrid = await r.search(mode=RetrievalMode.HYBRID, **kwargs)
    reranked = await r.search(mode=RetrievalMode.HYBRID_RERANK, **kwargs)
    assert hybrid.chunks[0].hit.text != "alpha zulu"
    assert reranked.chunks[0].hit.text == "alpha zulu"
    assert {c.hit.chunk_id for c in hybrid.chunks} == {c.hit.chunk_id for c in reranked.chunks}
    assert ranker.calls == 1  # plain hybrid never calls the cross-encoder


async def test_only_the_head_is_reranked_and_the_fused_tail_still_fills_top_k(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    uid = await me(client, await new_user(client))
    texts = ["alpha alpha alpha", "alpha alpha beta", "alpha beta gamma", "alpha zulu"]
    doc = await seed_document(infra, uid, "d.pdf", texts)
    ranker = KeywordReranker()
    ranker.prefer = "zulu"
    settings = get_settings().model_copy(update={"rerank_candidates": 2})
    r = retriever(infra, settings=settings, reranker=ranker)
    kwargs: dict[str, Any] = {"user_id": uid, "query": "alpha", "document_ids": [doc], "top_k": 4}
    fused = await r.search(mode=RetrievalMode.HYBRID, **kwargs)
    result = await r.search(mode=RetrievalMode.HYBRID_RERANK, **kwargs)
    assert len(result.chunks) == 4
    assert [c.rerank_score is not None for c in result.chunks] == [True, True, False, False]
    fused_ids = [c.hit.chunk_id for c in fused.chunks]
    # the head is a permutation of the first two fused chunks; the tail keeps the fused order
    assert {c.hit.chunk_id for c in result.chunks[:2]} == set(fused_ids[:2])
    assert [c.hit.chunk_id for c in result.chunks[2:]] == fused_ids[2:]


async def test_rerank_runs_off_the_event_loop(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    headers = await new_user(client)
    uid = await me(client, headers)
    doc = await seed_document(infra, uid, "d.pdf", ["alpha one", "alpha two"])
    ranker = KeywordReranker()
    ranker.delay_s = 0.4
    worst_lag = 0.0
    stop = asyncio.Event()

    async def heartbeat() -> None:
        nonlocal worst_lag
        while not stop.is_set():
            before = time.perf_counter()
            await asyncio.sleep(0.01)
            worst_lag = max(worst_lag, time.perf_counter() - before - 0.01)

    task = asyncio.create_task(heartbeat())
    result = await retriever(infra, reranker=ranker).search(
        user_id=uid,
        query="alpha",
        document_ids=[doc],
        mode=RetrievalMode.HYBRID_RERANK,
        top_k=2,
    )
    stop.set()
    await task
    assert result.timings_ms["rerank"] >= 400
    assert ranker.threads and ranker.threads[0].startswith("inference")
    assert worst_lag < 0.2, f"event loop blocked for {worst_lag * 1000:.0f} ms"


async def test_rerank_timeout_falls_back_to_the_fused_order(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    headers = await new_user(client)
    uid = await me(client, headers)
    doc = await seed_document(infra, uid, "d.pdf", ["alpha alpha one", "alpha two"])
    ranker = KeywordReranker()
    ranker.delay_s = 0.6
    settings = get_settings().model_copy(update={"rerank_timeout_s": 0.1})
    kwargs: dict[str, Any] = {"user_id": uid, "query": "alpha", "document_ids": [doc], "top_k": 5}
    slow = await retriever(infra, settings=settings, reranker=ranker).search(
        mode=RetrievalMode.HYBRID_RERANK, **kwargs
    )
    plain = await retriever(infra).search(mode=RetrievalMode.HYBRID, **kwargs)
    assert slow.degraded == ["rerank_timeout"]
    assert [c.hit.chunk_id for c in slow.chunks] == [c.hit.chunk_id for c in plain.chunks]
    assert all(c.rerank_score is None for c in slow.chunks)


async def test_reranker_failure_degrades_instead_of_failing_the_search(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    headers = await new_user(client)
    uid = await me(client, headers)
    doc = await seed_document(infra, uid, "d.pdf", ["alpha one", "alpha two"])
    ranker = KeywordReranker()
    ranker.fail = True
    result = await retriever(infra, reranker=ranker).search(
        user_id=uid,
        query="alpha",
        document_ids=[doc],
        mode=RetrievalMode.HYBRID_RERANK,
        top_k=5,
    )
    assert result.degraded == ["rerank_unavailable"]
    assert len(result.chunks) == 2


# ------------------------------------------------------------------ partial failures


class SparseDown(HashEmbedder):
    def embed_query_sparse(self, text: str) -> SparseVec:
        raise ConnectionError("sparse model unavailable")


class BothDown(SparseDown):
    def embed_query_dense(self, text: str) -> list[float]:
        raise ConnectionError("dense model unavailable")


async def test_hybrid_survives_one_failing_ranker_and_says_so(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    headers = await new_user(client)
    uid = await me(client, headers)
    doc = await seed_document(infra, uid, "d.pdf", ["alpha one", "beta two"])
    result = await retriever(infra, embedder=SparseDown(get_settings())).search(
        user_id=uid, query="alpha", document_ids=[doc], mode=RetrievalMode.HYBRID, top_k=5
    )
    assert result.degraded == ["sparse_unavailable"]
    assert result.chunks and all(c.sparse_rank is None for c in result.chunks)


async def test_search_fails_cleanly_when_every_ranker_fails(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    headers = await new_user(client)
    uid = await me(client, headers)
    doc = await seed_document(infra, uid, "d.pdf", ["alpha one"])
    with pytest.raises(ExternalServiceUnavailableError):
        await retriever(infra, embedder=BothDown(get_settings())).search(
            user_id=uid, query="alpha", document_ids=[doc], mode=RetrievalMode.HYBRID, top_k=5
        )


async def test_slow_vector_search_times_out_with_a_stable_error(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    headers = await new_user(client)
    uid = await me(client, headers)
    doc = await seed_document(infra, uid, "d.pdf", ["alpha one"])

    class SlowEmbedder(HashEmbedder):
        def embed_query_dense(self, text: str) -> list[float]:
            time.sleep(0.5)
            return super().embed_query_dense(text)

    settings = get_settings().model_copy(update={"retrieval_timeout_s": 0.1})
    with pytest.raises(RetrievalTimeoutError):
        await retriever(infra, settings=settings, embedder=SlowEmbedder(settings)).search(
            user_id=uid, query="alpha", document_ids=[doc], mode=RetrievalMode.DENSE, top_k=5
        )


async def test_no_readable_documents_never_touches_the_vector_store(
    infra: Infrastructure,
) -> None:
    class Boom:
        async def query_points(self, *args: Any, **kwargs: Any) -> Any:
            raise AssertionError("the vector store must not be queried")

    r = Retriever(
        Boom(),  # pyright: ignore[reportArgumentType]
        get_settings(),
        HashEmbedder(get_settings()),
        KeywordReranker(),
        infra.inference_executor,
    )
    result = await r.search(
        user_id=uuid.uuid4(), query="alpha", document_ids=[], mode=RetrievalMode.HYBRID, top_k=5
    )
    assert result.chunks == []


# ------------------------------------------------------------------ authorization


async def test_the_vector_filter_alone_keeps_other_users_chunks_out(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    """Even if a caller wrongly passes another user's document ids, the tenant filter holds."""
    alice = await me(client, await new_user(client, "alice@example.com"))
    bob = await me(client, await new_user(client, "bob@example.com"))
    bob_doc = await seed_document(infra, bob, "bob.pdf", ["alpha secret of bob"])
    for mode in RetrievalMode:
        result = await retriever(infra).search(
            user_id=alice, query="alpha secret", document_ids=[bob_doc], mode=mode, top_k=10
        )
        assert result.chunks == [], mode


async def test_every_vector_query_carries_the_tenant_and_document_filter(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    uid = await me(client, await new_user(client))
    doc = await seed_document(infra, uid, "d.pdf", ["alpha one"])
    seen: list[tuple[str, Any]] = []

    class Spy:
        async def query_points(self, *args: Any, **kwargs: Any) -> Any:
            seen.append((kwargs["using"], kwargs["query_filter"]))
            return await infra.qdrant.query_points(*args, **kwargs)

    r = Retriever(
        Spy(),  # pyright: ignore[reportArgumentType]
        get_settings(),
        HashEmbedder(get_settings()),
        KeywordReranker(),
        infra.inference_executor,
    )
    for mode in RetrievalMode:
        seen.clear()
        await r.search(user_id=uid, query="alpha", document_ids=[doc], mode=mode, top_k=5)
        expected = {"dense"} if mode is RetrievalMode.DENSE else {"sparse"}
        if mode in (RetrievalMode.HYBRID, RetrievalMode.HYBRID_RERANK):
            expected = {"dense", "sparse"}
        assert {using for using, _ in seen} == expected, mode
        for _, flt in seen:
            keys = {c.key for c in flt.must}
            assert keys == {"user_id", "document_id"}, mode


async def test_the_readable_document_lookup_is_scoped_to_the_owner(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    from app.repositories.documents import DocumentRepository

    alice = await me(client, await new_user(client, "alice@example.com"))
    bob = await me(client, await new_user(client, "bob@example.com"))
    a_doc = await seed_document(infra, alice, "a.pdf", ["alpha"])
    b_doc = await seed_document(infra, bob, "b.pdf", ["alpha"])
    async with infra.session_factory() as session:
        repo = DocumentRepository(session)
        assert set(await repo.readable(alice)) == {a_doc}
        assert await repo.readable(alice, [b_doc]) == {}
        assert set(await repo.readable(alice, [a_doc, b_doc])) == {a_doc}


async def test_search_only_returns_the_callers_own_documents(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    ha = await new_user(client, "alice@example.com")
    hb = await new_user(client, "bob@example.com")
    alice, bob = await me(client, ha), await me(client, hb)
    a_doc = await seed_document(infra, alice, "alice.pdf", ["the launch window is tuesday"])
    b_doc = await seed_document(infra, bob, "bob.pdf", ["the launch window is friday"])
    for headers, own, other in ((ha, a_doc, b_doc), (hb, b_doc, a_doc)):
        resp = await search(client, headers, "launch window")
        assert resp.status_code == 200, resp.text
        ids = {r["document_id"] for r in resp.json()["results"]}
        assert ids == {str(own)} and str(other) not in resp.text


@pytest.mark.parametrize(
    "status", ["pending", "validating", "queued", "processing", "extracting", "failed"]
)
async def test_documents_that_are_not_completed_are_never_searched(
    client: httpx2.AsyncClient, infra: Infrastructure, status: str
) -> None:
    headers = await new_user(client)
    uid = await me(client, headers)
    await seed_document(infra, uid, "unfinished.pdf", ["alpha only here"], status=status)
    resp = await search(client, headers, "alpha")
    assert resp.status_code == 200 and resp.json()["results"] == []


async def test_deleted_documents_are_never_searched_even_before_their_vectors_are_purged(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    headers = await new_user(client)
    uid = await me(client, headers)
    await seed_document(infra, uid, "gone.pdf", ["alpha forgotten"], deleted=True)
    keep = await seed_document(infra, uid, "kept.pdf", ["alpha remembered"])
    resp = await search(client, headers, "alpha")
    assert [r["document_id"] for r in resp.json()["results"]] == [str(keep)]


async def test_document_scope_narrows_results_and_hides_unreadable_ids(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    ha = await new_user(client, "alice@example.com")
    hb = await new_user(client, "bob@example.com")
    alice, bob = await me(client, ha), await me(client, hb)
    d1 = await seed_document(infra, alice, "one.pdf", ["alpha in the first"])
    await seed_document(infra, alice, "two.pdf", ["alpha in the second"])
    foreign = await seed_document(infra, bob, "bob.pdf", ["alpha in bobs"])
    narrowed = await search(client, ha, "alpha", document_ids=[str(d1)])
    assert [r["document_id"] for r in narrowed.json()["results"]] == [str(d1)]
    # a foreign id and an unknown id behave identically: nothing, no error
    foreign_scope = await search(client, ha, "alpha", document_ids=[str(foreign)])
    unknown_scope = await search(client, ha, "alpha", document_ids=[str(uuid.uuid4())])
    assert foreign_scope.status_code == unknown_scope.status_code == 200
    assert foreign_scope.json()["results"] == unknown_scope.json()["results"] == []


async def test_a_reindexed_or_stale_vector_is_dropped_by_the_final_database_check(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    headers = await new_user(client)
    uid = await me(client, headers)
    # the vectors belong to version 1 but the document is already at version 2
    await seed_document(infra, uid, "stale.pdf", ["alpha stale"], doc_version=2, index_version=1)
    resp = await search(client, headers, "alpha")
    assert resp.status_code == 200 and resp.json()["results"] == []


async def test_a_document_deleted_while_searching_is_dropped_before_returning(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    headers = await new_user(client)
    uid = await me(client, headers)
    doc = await seed_document(infra, uid, "race.pdf", ["alpha race"])

    class DeletingEmbedder(HashEmbedder):
        """Marks the document deleted after authorization but before the search returns."""

        def embed_query_dense(self, text: str) -> list[float]:
            with infra.sync_session_factory() as session:
                session.execute(
                    sa_text("UPDATE documents SET deleted_at = now() WHERE id = :id"), {"id": doc}
                )
                session.commit()
            return super().embed_query_dense(text)

    set_embedder_factory(DeletingEmbedder)
    resp = await search(client, headers, "alpha")
    assert resp.status_code == 200 and resp.json()["results"] == []


# ------------------------------------------------------------------ API contract


async def test_empty_knowledge_base_returns_an_empty_result(client: httpx2.AsyncClient) -> None:
    headers = await new_user(client)
    resp = await search(client, headers, "anything at all")
    assert resp.status_code == 200
    assert resp.json()["results"] == [] and resp.json()["degraded"] == []


async def test_search_response_is_citation_ready_and_leaks_no_internals(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    headers = await new_user(client)
    uid = await me(client, headers)
    doc = await seed_document(
        infra, uid, "handbook.pdf", ["alpha first page", "alpha second page"], section="Part A"
    )
    resp = await search(client, headers, "alpha")
    body = resp.json()
    assert body["mode"] == "hybrid_rerank" and set(body["timings_ms"]) >= {"search", "total"}
    first = body["results"][0]
    assert set(first) == {
        "chunk_id",
        "document_id",
        "document_name",
        "page_number",
        "page_end",
        "section_path",
        "heading",
        "text",
        "scores",
    }
    assert first["document_id"] == str(doc) and first["document_name"] == "handbook.pdf"
    assert first["section_path"] == "Part A" and first["text"].startswith("alpha")
    assert {r["page_number"] for r in body["results"]} == {1, 2}
    for secret in ("user_id", "storage_key", str(uid)):
        assert secret not in resp.text


async def test_search_requires_a_valid_token(client: httpx2.AsyncClient) -> None:
    resp = await client.post(f"{API}/search", json={"query": "alpha"})
    assert resp.status_code == 401
    resp = await client.post(
        f"{API}/search", json={"query": "alpha"}, headers=bearer("not-a-token")
    )
    assert resp.status_code == 401


@pytest.mark.parametrize(
    "body",
    [
        {"query": ""},
        {"query": "   "},
        {"query": "x" * 1001},
        {"query": "ok", "top_k": 0},
        {"query": "ok", "top_k": 101},
        {"query": "ok", "document_ids": ["not-a-uuid"]},
        {"query": "ok", "user_id": str(uuid.uuid4())},
        {"query": "ok", "document_ids": [str(uuid.uuid4()) for _ in range(51)]},
    ],
)
async def test_invalid_search_requests_are_rejected_with_422(
    client: httpx2.AsyncClient, body: dict[str, Any]
) -> None:
    headers = await new_user(client)
    resp = await client.post(f"{API}/search", json=body, headers=headers)
    assert resp.status_code in (400, 422), resp.text
    assert resp.json()["error"]["request_id"]


async def test_search_is_rate_limited_per_user(
    client_with_settings: Any, infra: Infrastructure
) -> None:
    settings = get_settings().model_copy(update={"rl_search_limit": 3})
    client: httpx2.AsyncClient = await client_with_settings(settings)
    headers = await new_user(client)
    codes = [(await search(client, headers, "alpha")).status_code for _ in range(5)]
    assert codes == [200, 200, 200, 429, 429]
    other = await new_user(client, "other@example.com")
    assert (await search(client, other, "alpha")).status_code == 200  # limits are per user


async def test_unverified_email_cannot_search_when_verification_is_required(
    client_with_settings: Any,
) -> None:
    settings = get_settings().model_copy(update={"require_verified_email": True})
    client: httpx2.AsyncClient = await client_with_settings(settings)
    headers = await new_user(client)
    resp = await search(client, headers, "alpha")
    assert resp.status_code == 403 and resp.json()["error"]["code"] == "EMAIL_NOT_VERIFIED"


async def test_oversized_scope_and_query_limits_follow_the_settings(
    client_with_settings: Any,
) -> None:
    settings = get_settings().model_copy(
        update={"retrieval_max_query_chars": 20, "retrieval_max_scope_documents": 2}
    )
    client: httpx2.AsyncClient = await client_with_settings(settings)
    headers = await new_user(client)
    long_query = await search(client, headers, "q" * 21)
    assert long_query.status_code == 422
    assert long_query.json()["error"]["details"][0]["code"] == "query_too_long"
    many = await search(client, headers, "ok", document_ids=[str(uuid.uuid4()) for _ in range(3)])
    assert many.status_code == 422
    assert many.json()["error"]["details"][0]["code"] == "too_many_documents"


# ------------------------------------------------------------------ model warm-up


async def test_warmup_loads_every_model_and_never_raises(infra: Infrastructure) -> None:
    from app.retrieval.warmup import warm_models

    ranker = KeywordReranker()
    set_reranker_factory(lambda _settings: ranker)
    assert await warm_models(get_settings(), infra.inference_executor) is True
    assert ranker.calls == 1

    class Broken(HashEmbedder):
        def embed_query_dense(self, text: str) -> list[float]:
            raise OSError("model files missing")

    set_embedder_factory(Broken)
    assert await warm_models(get_settings(), infra.inference_executor) is False


async def test_the_app_warms_models_in_the_background_when_configured(
    infra: Infrastructure,
) -> None:
    from app.main import create_app

    ranker = KeywordReranker()
    set_reranker_factory(lambda _settings: ranker)
    settings = get_settings().model_copy(update={"warm_models_on_startup": True})
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        for _ in range(100):
            if ranker.calls:
                break
            await asyncio.sleep(0.05)
    assert ranker.calls == 1
