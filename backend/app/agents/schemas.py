"""Contracts of the agent: what the model may produce (`DraftAnswer`) and what users receive
(`RAGResponse`).

The model only ever sees short source labels (S1, S2, ...), never chunk ids or document ids, and
it never supplies document names or excerpts that are trusted: the server builds every
`Citation` from stored data after verifying the source (see agents/citations.py). Both contracts
reject unknown fields. `RAGResponse` is versioned and identical for streaming and
streaming and non-streaming delivery.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = "1.0"
AnswerSupport = Literal["supported", "inference", "not_found", "none"]


class DraftCitation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str = Field(pattern=r"^S[0-9]{1,3}$", description="A source label such as S1")
    quote: str = Field(min_length=1, max_length=600, description="Words copied from that source")
    # optional claim about the page; verified against stored metadata, never trusted
    page: int | None = Field(default=None, ge=1, le=100000)


class DraftAnswer(BaseModel):
    """The JSON the model must return."""

    model_config = ConfigDict(extra="forbid")

    answer: str = Field(min_length=1, max_length=8000)
    citations: list[DraftCitation] = Field(default_factory=list[DraftCitation], max_length=12)
    insufficient_evidence: bool = False
    clarification_needed: bool = False
    follow_up_questions: list[str] = Field(default_factory=list[str], max_length=3)
    support: Literal["supported", "inference", "not_found"] = "supported"


class Citation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: str
    document_name: str
    page_number: int | None = None
    chunk_id: str
    excerpt: str


class AgentMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    execution_mode: Literal["single"]
    model_name: str
    retrieval_used: bool
    reranking_used: bool
    answer_support: AnswerSupport = "none"
    cache_status: Literal["hit", "miss", "bypass", "error", "none"] = "none"
    llm_calls: int = 0
    prompt_tokens: int = 0  # tokens actually spent (a cache hit spends none)
    completion_tokens: int = 0
    latency_ms: int = 0  # until the answer was ready (before saving)
    degraded: list[str] = Field(default_factory=list[str])
    route: str = ""


class RAGResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = SCHEMA_VERSION
    answer: str
    citations: list[Citation] = Field(default_factory=list[Citation])
    insufficient_evidence: bool = False
    clarification_needed: bool = False
    follow_up_questions: list[str] = Field(default_factory=list[str])
    metadata: AgentMetadata


def draft_json_schema() -> dict[str, object]:
    """JSON schema sent to providers that support native structured output."""
    return DraftAnswer.model_json_schema()
