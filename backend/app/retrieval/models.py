"""Plain data carried through retrieval. Never ORM objects, never raw Qdrant points."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import StrEnum


class RetrievalMode(StrEnum):
    DENSE = "dense"
    SPARSE = "sparse"
    HYBRID = "hybrid"  # dense + sparse fused with RRF
    HYBRID_RERANK = "hybrid_rerank"  # hybrid, then a cross-encoder over the fused candidates


@dataclass(frozen=True, slots=True)
class Hit:
    """One stored chunk as returned by the vector store (payload fields only)."""

    chunk_id: str
    document_id: uuid.UUID
    document_version: int
    chunk_index: int
    filename: str
    page_number: int | None
    page_end: int | None
    section_path: str
    heading: str | None
    text: str


@dataclass(slots=True)
class RankedChunk:
    hit: Hit
    dense_rank: int | None = None
    sparse_rank: int | None = None
    dense_score: float | None = None
    sparse_score: float | None = None
    fused_score: float | None = None
    rerank_score: float | None = None
    # the document's authoritative name from PostgreSQL (filled by the service)
    document_name: str = ""


@dataclass(slots=True)
class RetrievalResult:
    query: str
    mode: RetrievalMode
    chunks: list[RankedChunk]
    timings_ms: dict[str, float] = field(default_factory=dict[str, float])
    # soft failures that were handled, e.g. "sparse_unavailable", "rerank_timeout"
    degraded: list[str] = field(default_factory=list[str])
    candidates: dict[str, int] = field(default_factory=dict[str, int])
    # how many completed documents the user can search (0 = nothing uploaded or processed yet)
    readable_documents: int = 0
    # True when the cross-encoder was not run because fusion was already confident
    # (app.retrieval.skip)
    rerank_skipped: bool = False

    @property
    def evidence_chunk_ids(self) -> frozenset[str]:
        """The authorized evidence set of this execution (used later to verify citations)."""
        return frozenset(c.hit.chunk_id for c in self.chunks)
