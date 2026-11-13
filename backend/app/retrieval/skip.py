"""When may the cross-encoder be skipped? Only when fusion is already confident (KI-023, ADR-038).

The decision uses nothing but the two ranked lists the fusion step already has: the best fused
chunk is rank 1 in BOTH the dense and the sparse list (two independent rankers agree) and its RRF
score is clearly ahead of the runner-up. The gap is RELATIVE, `(first - second) / first`, so the
threshold does not depend on the RRF constant k. `RERANK_SKIP_MARGIN=0` disables the rule.

Whether a margin keeps quality is an empirical question; `evaluation/retrieval/rerank_bench.py`
measures it, and the default in `Settings` is the value that benchmark supports.
"""

from __future__ import annotations

from collections.abc import Sequence

from app.retrieval.models import RankedChunk


def relative_margin(ordered: Sequence[RankedChunk]) -> float | None:
    """The relative RRF gap between the first two candidates (None if it cannot be computed)."""
    if len(ordered) < 2:
        return None
    first = ordered[0].fused_score or 0.0
    second = ordered[1].fused_score or 0.0
    if first <= 0.0:
        return None
    return (first - second) / first


def should_skip_rerank(ordered: Sequence[RankedChunk], margin: float) -> bool:
    """True when the fused order is trusted without running the cross-encoder."""
    if margin <= 0.0 or not ordered:
        return False
    first = ordered[0]
    if first.dense_rank != 1 or first.sparse_rank != 1:
        return False
    if len(ordered) == 1:
        return True  # nothing to reorder
    gap = relative_margin(ordered)
    return gap is not None and gap >= margin
