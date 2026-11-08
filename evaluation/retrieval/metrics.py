"""Ranking metrics with binary relevance. Pure functions; unit-tested in backend/tests/unit.

`ranked` is the list of ids a retriever returned, best first. `relevant` is the ground-truth set.
Queries with an empty relevant set are not scoreable and must be excluded by the caller.
"""

from __future__ import annotations

import math
from collections.abc import Collection, Sequence


def hit_at_k(ranked: Sequence[str], relevant: Collection[str], k: int) -> float:
    return 1.0 if any(item in relevant for item in ranked[:k]) else 0.0


def recall_at_k(ranked: Sequence[str], relevant: Collection[str], k: int) -> float:
    if not relevant:
        raise ValueError("no relevant items: the query is not scoreable")
    found = len({item for item in ranked[:k] if item in relevant})
    return found / len(set(relevant))


def precision_at_k(ranked: Sequence[str], relevant: Collection[str], k: int) -> float:
    if k < 1:
        raise ValueError("k must be >= 1")
    # the denominator is k even when fewer than k items were returned
    return sum(1 for item in ranked[:k] if item in relevant) / k


def reciprocal_rank(ranked: Sequence[str], relevant: Collection[str]) -> float:
    for position, item in enumerate(ranked, start=1):
        if item in relevant:
            return 1.0 / position
    return 0.0


def ndcg_at_k(ranked: Sequence[str], relevant: Collection[str], k: int) -> float:
    if not relevant:
        raise ValueError("no relevant items: the query is not scoreable")
    seen: set[str] = set()
    dcg = 0.0
    for position, item in enumerate(ranked[:k], start=1):
        if item in relevant and item not in seen:
            seen.add(item)
            dcg += 1.0 / math.log2(position + 1)
    ideal = sum(1.0 / math.log2(p + 1) for p in range(1, min(len(set(relevant)), k) + 1))
    return dcg / ideal


def summarize(
    runs: Sequence[tuple[Sequence[str], Collection[str]]], ks: Sequence[int]
) -> dict[str, float]:
    """Mean of every metric over scoreable queries (empty relevant sets are skipped)."""
    scoreable = [(r, rel) for r, rel in runs if rel]
    if not scoreable:
        raise ValueError("no scoreable queries")
    n = len(scoreable)
    out: dict[str, float] = {"queries": float(n)}
    for k in ks:
        out[f"hit@{k}"] = sum(hit_at_k(r, rel, k) for r, rel in scoreable) / n
        out[f"recall@{k}"] = sum(recall_at_k(r, rel, k) for r, rel in scoreable) / n
        out[f"precision@{k}"] = sum(precision_at_k(r, rel, k) for r, rel in scoreable) / n
        out[f"ndcg@{k}"] = sum(ndcg_at_k(r, rel, k) for r, rel in scoreable) / n
    out["mrr"] = sum(reciprocal_rank(r, rel) for r, rel in scoreable) / n
    return out
