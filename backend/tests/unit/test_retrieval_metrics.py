"""The evaluation metrics, checked against hand-computed values."""

from __future__ import annotations

import math

import pytest

from evaluation.retrieval.metrics import (
    hit_at_k,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
    reciprocal_rank,
    summarize,
)

RANKED = ["a", "b", "c", "d", "e"]


def test_hit_at_k() -> None:
    assert hit_at_k(RANKED, {"c"}, 3) == 1.0
    assert hit_at_k(RANKED, {"c"}, 2) == 0.0
    assert hit_at_k(RANKED, {"zzz"}, 5) == 0.0
    assert hit_at_k([], {"a"}, 5) == 0.0


def test_recall_and_precision_at_k() -> None:
    relevant = {"b", "d", "x"}
    assert recall_at_k(RANKED, relevant, 4) == pytest.approx(2 / 3)
    assert recall_at_k(RANKED, relevant, 1) == 0.0
    assert precision_at_k(RANKED, relevant, 4) == pytest.approx(2 / 4)
    assert precision_at_k(RANKED, relevant, 2) == pytest.approx(1 / 2)
    # fewer results than k: the denominator stays k
    assert precision_at_k(["b"], relevant, 5) == pytest.approx(1 / 5)


def test_duplicates_in_the_ranking_do_not_inflate_recall() -> None:
    assert recall_at_k(["b", "b", "b"], {"b", "x"}, 3) == pytest.approx(1 / 2)


def test_reciprocal_rank() -> None:
    assert reciprocal_rank(RANKED, {"c", "e"}) == pytest.approx(1 / 3)
    assert reciprocal_rank(RANKED, {"a"}) == 1.0
    assert reciprocal_rank(RANKED, {"zzz"}) == 0.0


def test_ndcg_matches_the_definition() -> None:
    # relevant at ranks 2 and 4, two relevant overall
    dcg = 1 / math.log2(3) + 1 / math.log2(5)
    ideal = 1 / math.log2(2) + 1 / math.log2(3)
    assert ndcg_at_k(RANKED, {"b", "d"}, 5) == pytest.approx(dcg / ideal)
    assert ndcg_at_k(["a", "b"], {"a", "b"}, 2) == pytest.approx(1.0)
    assert ndcg_at_k(RANKED, {"zzz"}, 5) == 0.0


def test_ndcg_ideal_is_capped_by_k_and_ignores_repeats() -> None:
    # three relevant items but k=2: a perfect top-2 scores 1.0
    assert ndcg_at_k(["a", "b", "z"], {"a", "b", "c"}, 2) == pytest.approx(1.0)
    assert ndcg_at_k(["a", "a"], {"a", "b"}, 2) < 1.0


def test_unscoreable_queries_raise_and_are_skipped_by_summarize() -> None:
    with pytest.raises(ValueError):
        recall_at_k(RANKED, set(), 3)
    with pytest.raises(ValueError):
        ndcg_at_k(RANKED, set(), 3)
    result = summarize([(RANKED, {"a"}), (RANKED, set())], ks=[1])
    assert result["queries"] == 1.0 and result["hit@1"] == 1.0
    with pytest.raises(ValueError):
        summarize([(RANKED, set())], ks=[1])


def test_summarize_averages_over_queries() -> None:
    runs = [(["a", "b"], {"a"}), (["a", "b"], {"b"})]
    s = summarize(runs, ks=[1, 2])
    assert s["hit@1"] == 0.5 and s["hit@2"] == 1.0
    assert s["mrr"] == pytest.approx((1 + 0.5) / 2)
    assert s["precision@2"] == pytest.approx(0.5)
    assert s["recall@1"] == 0.5
