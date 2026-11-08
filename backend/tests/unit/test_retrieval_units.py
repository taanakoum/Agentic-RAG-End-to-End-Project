"""Pure retrieval logic: RRF, the authorization filter, payload guards, result helpers."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from qdrant_client.models import FieldCondition, MatchAny, MatchValue, ScoredPoint

from app.core.config import Settings
from app.retrieval.fusion import rrf_fuse
from app.retrieval.models import Hit, RankedChunk, RetrievalMode, RetrievalResult
from app.retrieval.retriever import _hit_from_point, build_filter  # pyright: ignore[reportPrivateUsage]

# ------------------------------------------------------------------ RRF


def test_rrf_scores_follow_the_formula_with_ranks_starting_at_one() -> None:
    fused = dict(rrf_fuse({"dense": ["a", "b", "c"], "sparse": ["b", "a"]}, k=60))
    assert fused["a"] == pytest.approx(1 / 61 + 1 / 62)
    assert fused["b"] == pytest.approx(1 / 62 + 1 / 61)
    assert fused["c"] == pytest.approx(1 / 63)


def test_rrf_orders_by_score_and_merges_duplicates_across_rankers() -> None:
    fused = rrf_fuse({"dense": ["a", "b", "c"], "sparse": ["c", "d"]}, k=60)
    ids = [i for i, _ in fused]
    assert sorted(ids) == ["a", "b", "c", "d"]  # deduplicated: c appears once
    scores = [s for _, s in fused]
    assert scores == sorted(scores, reverse=True)
    assert ids[0] == "c"  # found by both rankers


def test_rrf_includes_candidates_found_by_only_one_ranker() -> None:
    fused = {i for i, _ in rrf_fuse({"dense": ["a"], "sparse": ["b"]}, k=60)}
    assert fused == {"a", "b"}


def test_rrf_counts_a_repeated_id_once_per_ranker() -> None:
    once = dict(rrf_fuse({"dense": ["a", "b"]}, k=60))
    repeated = dict(rrf_fuse({"dense": ["a", "a", "b"]}, k=60))
    assert repeated["a"] == once["a"]
    assert repeated["b"] == pytest.approx(1 / 62)  # still rank 2: duplicates do not shift ranks


def test_rrf_is_deterministic_on_ties() -> None:
    first = rrf_fuse({"dense": ["x"], "sparse": ["y"]}, k=60)
    second = rrf_fuse({"sparse": ["y"], "dense": ["x"]}, k=60)
    assert first == second == [("x", 1 / 61), ("y", 1 / 61)]


def test_rrf_k_controls_how_much_top_ranks_dominate() -> None:
    small = dict(rrf_fuse({"r": ["a", "b"]}, k=1))
    large = dict(rrf_fuse({"r": ["a", "b"]}, k=1000))
    assert small["a"] / small["b"] > large["a"] / large["b"]


def test_rrf_weights_and_validation() -> None:
    weighted = dict(rrf_fuse({"dense": ["a"], "sparse": ["b"]}, k=60, weights={"dense": 2.0}))
    assert weighted["a"] == pytest.approx(2 / 61) and weighted["b"] == pytest.approx(1 / 61)
    assert rrf_fuse({}, k=60) == [] and rrf_fuse({"dense": []}, k=60) == []
    with pytest.raises(ValueError):
        rrf_fuse({"dense": ["a"]}, k=0)


# ------------------------------------------------------------------ authorization filter


def test_the_filter_always_contains_the_tenant_and_the_document_scope() -> None:
    user = uuid.uuid4()
    docs = [uuid.uuid4(), uuid.uuid4(), uuid.uuid4()]
    flt = build_filter(user, [docs[0], docs[1], docs[0], docs[2]])
    must: list[Any] = list(flt.must or [])
    conditions = {c.key: c.match for c in must if isinstance(c, FieldCondition)}
    assert conditions["user_id"] == MatchValue(value=str(user))
    scope = conditions["document_id"]
    assert isinstance(scope, MatchAny)
    assert sorted(str(x) for x in scope.any) == sorted(str(d) for d in docs)
    assert not flt.should and not flt.must_not


# ------------------------------------------------------------------ payload guards


def _point(payload: dict[str, Any]) -> ScoredPoint:
    return ScoredPoint(id=str(uuid.uuid4()), version=1, score=0.5, payload=payload)


def _payload(user: uuid.UUID) -> dict[str, Any]:
    return {
        "user_id": str(user),
        "chunk_id": "c1",
        "document_id": str(uuid.uuid4()),
        "document_version": 2,
        "chunk_index": 3,
        "filename": "a.pdf",
        "page_number": 4,
        "page_end": 5,
        "section_path": "A > B",
        "heading": "B",
        "text": "hello",
    }


def test_a_well_formed_payload_becomes_a_hit() -> None:
    user = uuid.uuid4()
    hit = _hit_from_point(_point(_payload(user)), user)
    assert hit is not None
    assert (hit.chunk_id, hit.document_version, hit.page_number, hit.page_end) == ("c1", 2, 4, 5)
    assert hit.section_path == "A > B" and hit.text == "hello"


def test_a_chunk_owned_by_someone_else_is_dropped_even_if_the_filter_failed() -> None:
    owner, intruder = uuid.uuid4(), uuid.uuid4()
    assert _hit_from_point(_point(_payload(owner)), intruder) is None


@pytest.mark.parametrize("missing", ["chunk_id", "document_id", "text", "document_version"])
def test_malformed_payloads_are_dropped_not_crashed_on(missing: str) -> None:
    user = uuid.uuid4()
    payload = _payload(user)
    del payload[missing]
    assert _hit_from_point(_point(payload), user) is None


def test_a_payload_with_a_bad_document_id_is_dropped() -> None:
    user = uuid.uuid4()
    payload = _payload(user)
    payload["document_id"] = "not-a-uuid"
    assert _hit_from_point(_point(payload), user) is None


# ------------------------------------------------------------------ results and settings


def test_the_evidence_set_is_exactly_the_returned_chunks() -> None:
    def hit(chunk_id: str) -> Hit:
        return Hit(chunk_id, uuid.uuid4(), 1, 0, "a.pdf", 1, 1, "", None, "t")

    result = RetrievalResult(
        query="q",
        mode=RetrievalMode.HYBRID,
        chunks=[RankedChunk(hit("a")), RankedChunk(hit("b"))],
    )
    assert result.evidence_chunk_ids == frozenset({"a", "b"})
    assert RetrievalResult(query="q", mode=RetrievalMode.DENSE, chunks=[]).evidence_chunk_ids == (
        frozenset()
    )


def test_retrieval_defaults_are_consistent() -> None:
    s = Settings()
    assert s.retrieval_top_k <= s.retrieval_max_top_k
    assert s.rrf_k == 60 and s.rerank_enabled
    assert s.reranker_model == "BAAI/bge-reranker-v2-m3-int8"
