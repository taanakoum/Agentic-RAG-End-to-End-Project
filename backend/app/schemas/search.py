"""Search API contracts. Citation-ready metadata only; no internal Qdrant structures."""

from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict, Field


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=10000)
    document_ids: list[uuid.UUID] | None = Field(
        default=None, description="Restrict the search to these documents (must be readable)"
    )
    top_k: int | None = Field(default=None, ge=1, le=100)


class SearchScores(BaseModel):
    dense_rank: int | None
    sparse_rank: int | None
    dense_score: float | None
    sparse_score: float | None
    fused_score: float | None
    rerank_score: float | None


class SearchChunk(BaseModel):
    chunk_id: str
    document_id: uuid.UUID
    document_name: str
    page_number: int | None
    page_end: int | None
    section_path: str
    heading: str | None
    text: str
    scores: SearchScores


class SearchResponse(BaseModel):
    query: str
    mode: str
    results: list[SearchChunk]
    degraded: list[str] = Field(description="Handled soft failures, e.g. rerank_timeout")
    timings_ms: dict[str, float]
