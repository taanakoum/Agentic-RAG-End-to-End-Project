"""The adaptive re-rank skip rule and the re-ranker's thread setting (KI-023, ADR-038)."""

from __future__ import annotations

import sys
import types
import uuid
from typing import Any, ClassVar

import pytest

from app.core.config import get_settings
from app.retrieval.models import Hit, RankedChunk
from app.retrieval.rerank import FastEmbedReranker
from app.retrieval.skip import relative_margin, should_skip_rerank


def chunk(
    name: str, fused: float, dense: int | None = None, sparse: int | None = None
) -> RankedChunk:
    hit = Hit(name, uuid.uuid4(), 1, 0, "f.pdf", 1, 1, "", None, name)
    return RankedChunk(hit=hit, dense_rank=dense, sparse_rank=sparse, fused_score=fused)


K = 60
BOTH_FIRST = 2 / (K + 1)  # rank 1 in both lists
ONE_SECOND = 1 / (K + 2)  # rank 2 in one list


def test_confident_fusion_skips_the_cross_encoder() -> None:
    ordered = [chunk("a", BOTH_FIRST, 1, 1), chunk("b", ONE_SECOND, 2, None)]
    assert relative_margin(ordered) == pytest.approx(1 - ONE_SECOND / BOTH_FIRST)
    assert should_skip_rerank(ordered, 0.3)
    assert should_skip_rerank(ordered, 0.5)
    assert not should_skip_rerank(ordered, 0.6)  # margin 0.508 < 0.6


def test_a_disabled_margin_never_skips() -> None:
    ordered = [chunk("a", BOTH_FIRST, 1, 1), chunk("b", 0.0001, None, 2)]
    assert not should_skip_rerank(ordered, 0.0)


def test_the_top_chunk_must_be_first_in_both_rankers() -> None:
    far_ahead = 0.9 * BOTH_FIRST
    assert not should_skip_rerank([chunk("a", far_ahead, 1, 2), chunk("b", 0.001)], 0.1)
    assert not should_skip_rerank([chunk("a", far_ahead, 1, None), chunk("b", 0.001)], 0.1)
    assert not should_skip_rerank([chunk("a", far_ahead, None, 1), chunk("b", 0.001)], 0.1)


def test_a_narrow_lead_is_not_confident() -> None:
    # both rankers put "a" first and "b" second: the lead is 1/(k+2) relative, far below any margin
    ordered = [chunk("a", BOTH_FIRST, 1, 1), chunk("b", 2 / (K + 2), 2, 2)]
    assert relative_margin(ordered) == pytest.approx(1 / (K + 2))
    assert not should_skip_rerank(ordered, 0.1)


def test_a_single_candidate_has_nothing_to_reorder() -> None:
    assert should_skip_rerank([chunk("a", BOTH_FIRST, 1, 1)], 0.2)
    assert not should_skip_rerank([chunk("a", 0.01, 3, 1)], 0.2)
    assert not should_skip_rerank([], 0.2)
    assert relative_margin([chunk("a", 0.0, 1, 1), chunk("b", 0.0)]) is None


# ------------------------------------------------------------------ RERANK_THREADS plumbing


class _FakeEncoder:
    instances: ClassVar[list[dict[str, Any]]] = []

    def __init__(self, model_name: str, **kwargs: Any) -> None:
        self.instances.append({"model": model_name, **kwargs})

    def rerank(self, query: str, passages: list[str]) -> list[float]:
        return [float(len(p)) for p in passages]


@pytest.mark.parametrize(("threads", "expected"), [(0, None), (4, 4), (8, 8)])
def test_rerank_threads_reach_the_onnx_session(
    monkeypatch: pytest.MonkeyPatch, threads: int, expected: int | None
) -> None:
    _FakeEncoder.instances.clear()
    fake = types.ModuleType("fastembed.rerank.cross_encoder")
    fake.TextCrossEncoder = _FakeEncoder  # pyright: ignore[reportAttributeAccessIssue]
    monkeypatch.setitem(sys.modules, "fastembed.rerank.cross_encoder", fake)
    settings = get_settings().model_copy(update={"rerank_threads": threads})
    scores = FastEmbedReranker(settings).score("q", ["a", "bb"])
    assert scores == [1.0, 2.0]
    (created,) = _FakeEncoder.instances
    assert created["threads"] == expected and created["model"] == settings.reranker_model


def test_the_settings_defaults_are_conservative_and_validated() -> None:
    s = get_settings()
    assert s.rerank_threads >= 0 and 0.0 <= s.rerank_skip_margin <= 1.0
    with pytest.raises(ValueError, match="rerank_skip_margin"):
        type(s)(rerank_skip_margin=1.5)  # pyright: ignore[reportCallIssue]
