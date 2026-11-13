"""The retrieval cache (ADR-036): same question + same knowledge base = no second search, and never
across users, versions or deletions. Real PostgreSQL, Redis and Qdrant; only the embedder and the
cross-encoder are the usual test adapters (the reranker counts its calls)."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from typing import Any

import httpx2
import pytest

from app.core.config import Settings, get_settings
from app.infra.clients import create_async_redis
from app.infra.container import Infrastructure
from app.ingestion.embedding import set_embedder_factory
from app.retrieval.cache import (
    RetrievalCache,
    knowledge_base_fingerprint,
    normalize_query,
    purge_user_retrieval_cache,
)
from app.retrieval.models import RetrievalMode
from app.retrieval.rerank import get_reranker, set_reranker_factory
from app.retrieval.retriever import Retriever
from tests.fakes import HashEmbedder, KeywordReranker
from tests.integration.docs_helpers import API, new_user
from tests.integration.retrieval_helpers import seed_document, set_document
from tests.integration.test_retrieval import me, search

pytestmark = pytest.mark.integration

TEXTS = ["alpha bravo charlie", "alpha delta echo", "foxtrot golf hotel"]


@pytest.fixture(autouse=True)
def _adapters() -> Iterator[None]:
    set_embedder_factory(HashEmbedder)
    set_reranker_factory(KeywordReranker)
    yield
    set_embedder_factory(None)
    set_reranker_factory(None)


class Counter:
    """Counts the vector-store searches and the reranker calls made by the code under test."""

    def __init__(self, infra: Infrastructure, monkeypatch: pytest.MonkeyPatch) -> None:
        self.searches = 0
        self.scrolls = 0
        original_query: Any = infra.qdrant.query_points
        original_scroll: Any = infra.qdrant.scroll

        async def query_points(*args: Any, **kwargs: Any) -> Any:
            self.searches += 1
            return await original_query(*args, **kwargs)

        async def scroll(*args: Any, **kwargs: Any) -> Any:
            self.scrolls += 1
            return await original_scroll(*args, **kwargs)

        monkeypatch.setattr(infra.qdrant, "query_points", query_points)
        monkeypatch.setattr(infra.qdrant, "scroll", scroll)

    @staticmethod
    def reranks() -> int:
        reranker = get_reranker(get_settings())
        assert isinstance(reranker, KeywordReranker)
        return reranker.calls


@pytest.fixture
def counter(infra: Infrastructure, monkeypatch: pytest.MonkeyPatch) -> Counter:
    return Counter(infra, monkeypatch)


def ids(response: httpx2.Response) -> list[str]:
    assert response.status_code == 200, response.text
    return [r["chunk_id"] for r in response.json()["results"]]


async def keys_of(infra: Infrastructure, user: uuid.UUID | None = None) -> list[str]:
    from app.retrieval.cache import user_pattern

    pattern = user_pattern(user) if user else "retr:*"
    return [k.decode() async for k in infra.redis.scan_iter(match=pattern)]  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType, reportUnknownArgumentType]


async def stored(infra: Infrastructure, key: str) -> str:
    raw: Any = await infra.redis.get(key)
    assert raw is not None
    return raw.decode() if isinstance(raw, bytes) else str(raw)


# ------------------------------------------------------------------ the hit


async def test_a_repeated_question_costs_one_search_and_one_rerank(
    client: httpx2.AsyncClient, infra: Infrastructure, counter: Counter
) -> None:
    headers = await new_user(client)
    uid = await me(client, headers)
    await seed_document(infra, uid, "d.pdf", TEXTS)

    first = await search(client, headers, "alpha bravo")
    assert counter.searches == 2 and counter.reranks() == 1  # dense + sparse, one rerank
    second = await search(client, headers, "alpha bravo")
    assert counter.searches == 2 and counter.reranks() == 1  # served from the cache
    assert counter.scrolls == 1  # the payloads were re-loaded from Qdrant by id

    assert ids(first) == ids(second) and ids(first)
    assert first.json()["results"] == second.json()["results"]  # text, pages and all scores
    assert "cache" in second.json()["timings_ms"] and "cache" not in first.json()["timings_ms"]
    # the query is normalised: case and spacing do not change the question
    third = await search(client, headers, "  ALPHA   Bravo ")
    assert counter.searches == 2 and ids(third) == ids(first)
    # another top_k is another question
    await search(client, headers, "alpha bravo", top_k=2)
    assert counter.searches == 4


async def test_the_cache_holds_ranks_and_ids_never_chunk_text(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    headers = await new_user(client)
    uid = await me(client, headers)
    await seed_document(infra, uid, "secret-handbook.pdf", TEXTS)
    await search(client, headers, "alpha bravo")
    (key,) = await keys_of(infra, uid)
    value = await stored(infra, key)
    assert "charlie" not in value and "secret-handbook" not in value and "alpha" not in value
    assert json.loads(value)["chunks"]
    assert 0 < await infra.redis.ttl(key) <= get_settings().retrieval_cache_ttl_s
    assert uid.hex not in key and str(uid) not in key  # the user id is hashed in the key


# ------------------------------------------------------------------ invalidation


async def test_a_new_document_or_version_invalidates_the_cache(
    client: httpx2.AsyncClient, infra: Infrastructure, counter: Counter
) -> None:
    headers = await new_user(client)
    uid = await me(client, headers)
    doc = await seed_document(infra, uid, "d.pdf", TEXTS)
    await search(client, headers, "alpha bravo")
    await search(client, headers, "alpha bravo")
    assert counter.searches == 2

    # a new version of the document (the readable (id, version) pairs change)
    await set_document(infra, doc, version=2)
    await search(client, headers, "alpha bravo")
    assert counter.searches == 4, "a new document version must not be served from the cache"

    # another document becomes readable
    await seed_document(infra, uid, "e.pdf", ["alpha zulu yankee"])
    after = await search(client, headers, "alpha bravo")
    assert counter.searches == 6
    assert any(r["text"] == "alpha zulu yankee" for r in after.json()["results"])


async def test_a_deleted_document_never_comes_back_from_the_cache(
    client: httpx2.AsyncClient, infra: Infrastructure, counter: Counter
) -> None:
    headers = await new_user(client)
    uid = await me(client, headers)
    keep = await seed_document(infra, uid, "keep.pdf", ["alpha keep one"])
    gone = await seed_document(infra, uid, "gone.pdf", ["alpha gone two"])
    before = await search(client, headers, "alpha")
    assert {r["document_id"] for r in before.json()["results"]} == {str(keep), str(gone)}

    deleted = await client.delete(f"{API}/documents/{gone}", headers=headers)
    assert deleted.status_code in {200, 202, 204}, deleted.text
    after = await search(client, headers, "alpha")
    assert {r["document_id"] for r in after.json()["results"]} == {str(keep)}


async def test_a_cached_chunk_that_no_longer_exists_makes_the_entry_stale(
    client: httpx2.AsyncClient, infra: Infrastructure, counter: Counter
) -> None:
    headers = await new_user(client)
    uid = await me(client, headers)
    doc = await seed_document(infra, uid, "d.pdf", TEXTS)
    first = await search(client, headers, "alpha bravo")
    (key,) = await keys_of(infra, uid)
    entry = json.loads(await stored(infra, key))
    entry["chunks"][0]["id"] = str(uuid.uuid4())  # a chunk that is not in the index
    await infra.redis.set(key, json.dumps(entry), ex=60)

    searches = counter.searches
    again = await search(client, headers, "alpha bravo")
    assert counter.searches == searches + 2  # treated as a miss: a full search ran
    assert ids(again) == ids(first)
    assert {r["document_id"] for r in again.json()["results"]} == {str(doc)}


# ------------------------------------------------------------------ users


async def test_cached_rankings_never_cross_users(
    client: httpx2.AsyncClient, infra: Infrastructure, counter: Counter
) -> None:
    alice = await new_user(client, "alice@example.com")
    bob = await new_user(client, "bob@example.com")
    alice_id, bob_id = await me(client, alice), await me(client, bob)
    await seed_document(infra, alice_id, "alice.pdf", TEXTS)
    bob_doc = await seed_document(infra, bob_id, "bob.pdf", TEXTS)  # same text, other owner

    alice_ids = ids(await search(client, alice, "alpha bravo"))
    searches = counter.searches
    bob_response = await search(client, bob, "alpha bravo")
    assert counter.searches == searches + 2, "another user's cache entry must not be used"
    assert set(ids(bob_response)).isdisjoint(alice_ids)
    assert {r["document_id"] for r in bob_response.json()["results"]} == {str(bob_doc)}

    assert len(await keys_of(infra, alice_id)) == 1 and len(await keys_of(infra, bob_id)) == 1
    # a user without documents gets nothing, cached or not
    carol = await new_user(client, "carol@example.com")
    assert ids(await search(client, carol, "alpha bravo")) == []


async def test_a_restored_entry_is_filtered_by_the_owner_again(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    """Even if a cache entry were copied under another user's key, the reload refuses the chunks."""
    alice = await new_user(client, "alice@example.com")
    bob = await new_user(client, "bob@example.com")
    alice_id, bob_id = await me(client, alice), await me(client, bob)
    await seed_document(infra, alice_id, "alice.pdf", TEXTS)
    bob_doc = await seed_document(infra, bob_id, "bob.pdf", ["bob only text"])
    await search(client, alice, "alpha bravo")
    (alice_key,) = await keys_of(infra, alice_id)
    stolen = await stored(infra, alice_key)

    settings = get_settings()
    readable = {bob_doc: ("bob.pdf", 1)}
    bob_key = RetrievalCache(infra.redis, settings).key(
        user_id=bob_id,
        query="alpha bravo",
        mode=RetrievalMode.HYBRID_RERANK,
        top_k=settings.retrieval_top_k,
        knowledge_base=knowledge_base_fingerprint(readable),
    )
    await infra.redis.set(bob_key, stolen, ex=60)
    response = await search(client, bob, "alpha bravo")
    assert ids(response) == [] or all(
        r["document_id"] == str(bob_doc) for r in response.json()["results"]
    )
    assert not any(r["text"] in TEXTS for r in response.json()["results"])


# ------------------------------------------------------------------ failure and settings


def retriever(infra: Infrastructure, cache: RetrievalCache, settings: Settings) -> Retriever:
    return Retriever(
        infra.qdrant,
        settings,
        HashEmbedder(settings),
        get_reranker(settings),
        infra.inference_executor,
        cache=cache,
    )


async def test_an_unreachable_redis_still_answers_and_never_raises(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    headers = await new_user(client)
    uid = await me(client, headers)
    doc = await seed_document(infra, uid, "d.pdf", TEXTS)
    settings = get_settings().model_copy(update={"redis_url": "redis://127.0.0.1:1/0"})
    dead = create_async_redis(settings)
    try:
        r = retriever(infra, RetrievalCache(dead, settings), settings)
        for _ in range(2):  # a miss every time, but always the right answer
            result = await r.search(
                user_id=uid,
                query="alpha bravo",
                document_ids=[doc],
                mode=RetrievalMode.HYBRID_RERANK,
                top_k=3,
                knowledge_base="fingerprint",
            )
            assert next(c.hit.text for c in result.chunks) == "alpha bravo charlie"
    finally:
        await dead.aclose()


async def test_degraded_results_are_not_cached(
    client: httpx2.AsyncClient, infra: Infrastructure, counter: Counter
) -> None:
    headers = await new_user(client)
    uid = await me(client, headers)
    await seed_document(infra, uid, "d.pdf", TEXTS)
    reranker = get_reranker(get_settings())
    assert isinstance(reranker, KeywordReranker)
    reranker.fail = True
    degraded = await search(client, headers, "alpha bravo")
    assert degraded.json()["degraded"] == ["rerank_unavailable"]
    assert await keys_of(infra) == []  # not stored

    reranker.fail = False
    healthy = await search(client, headers, "alpha bravo")
    assert healthy.json()["degraded"] == [] and len(await keys_of(infra)) == 1


async def test_the_cache_can_be_switched_off(
    client_with_settings: Any, infra: Infrastructure, counter: Counter
) -> None:
    off = get_settings().model_copy(update={"retrieval_cache_enabled": False})
    client: httpx2.AsyncClient = await client_with_settings(off)
    headers = await new_user(client)
    uid = await me(client, headers)
    await seed_document(infra, uid, "d.pdf", TEXTS)
    await search(client, headers, "alpha bravo")
    await search(client, headers, "alpha bravo")
    assert counter.reranks() == 2 and await keys_of(infra) == []


# ------------------------------------------------------------------ account deletion and units


async def test_the_cache_is_removed_with_the_account(
    client: httpx2.AsyncClient, infra: Infrastructure, runtime: Any
) -> None:
    from app.ingestion.purge import _purge_llm_cache  # pyright: ignore[reportPrivateUsage]

    alice = await new_user(client, "alice@example.com")
    bob = await new_user(client, "bob@example.com")
    alice_id, bob_id = await me(client, alice), await me(client, bob)
    await seed_document(infra, alice_id, "a.pdf", TEXTS)
    await seed_document(infra, bob_id, "b.pdf", TEXTS)
    await search(client, alice, "alpha bravo")
    await search(client, bob, "alpha bravo")

    _purge_llm_cache(runtime, alice_id)  # the step of account deletion
    assert await keys_of(infra, alice_id) == [] and len(await keys_of(infra, bob_id)) == 1
    assert await purge_user_retrieval_cache(infra.redis, bob_id) == 1
    assert await purge_user_retrieval_cache(infra.redis, bob_id) == 0  # idempotent


def test_the_query_normal_form_and_the_fingerprint() -> None:
    assert normalize_query("  Hello\tWORLD \n") == normalize_query("hello world")
    a, b = uuid.uuid4(), uuid.uuid4()
    one = knowledge_base_fingerprint({a: ("x.pdf", 1), b: ("y.pdf", 1)})
    assert one == knowledge_base_fingerprint({b: ("renamed.pdf", 1), a: ("x.pdf", 1)})
    assert one != knowledge_base_fingerprint({a: ("x.pdf", 2), b: ("y.pdf", 1)})  # a new version
    assert one != knowledge_base_fingerprint({a: ("x.pdf", 1)})  # a document less
