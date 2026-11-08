"""Reciprocal Rank Fusion: score(d) = sum over rankers of 1 / (k + rank_r(d)); ranks start at 1."""

from __future__ import annotations

from collections.abc import Mapping, Sequence


def rrf_fuse(
    rankings: Mapping[str, Sequence[str]], *, k: int, weights: Mapping[str, float] | None = None
) -> list[tuple[str, float]]:
    """Fuse ranked id lists. Duplicate ids inside one list count once (their best rank).

    Ties are broken by the best (lowest) rank any ranker gave the id, then by id, so the order
    is deterministic. Returns (id, score) best first.
    """
    if k < 1:
        raise ValueError("k must be >= 1")
    scores: dict[str, float] = {}
    best_rank: dict[str, int] = {}
    for name, ranking in rankings.items():
        weight = 1.0 if weights is None else weights.get(name, 1.0)
        seen: set[str] = set()
        rank = 0
        for item in ranking:
            if item in seen:
                continue
            seen.add(item)
            rank += 1
            scores[item] = scores.get(item, 0.0) + weight / (k + rank)
            best_rank[item] = min(best_rank.get(item, rank), rank)
    return sorted(scores.items(), key=lambda kv: (-kv[1], best_rank[kv[0]], kv[0]))
