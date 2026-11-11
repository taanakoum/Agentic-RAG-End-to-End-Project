"""State and run-scoped context of the single-agent graph.

`AgentState` is what LangGraph checkpoints (PostgreSQL) for each turn. It deliberately holds NO
chunk text (only references), so a deleted document's content does not linger in checkpoint
tables, and every turn starts from a COMPLETE fresh state with no reducers: nothing carries over
from the previous turn through the checkpointer (PostgreSQL `messages` are the history).

`TurnContext` is everything non-serialisable a node needs (services as callables, the evidence
text, the event sink). It is passed with `context=` and is never checkpointed.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, TypedDict

from app.agents.citations import EvidenceItem, VerificationResult
from app.agents.context import HistoryTurn
from app.agents.schemas import DraftAnswer
from app.core.config import Settings
from app.llm.cache import LLMGateway
from app.retrieval.models import RetrievalResult


class EvidenceRef(TypedDict):
    label: str
    chunk_id: str
    document_id: str
    document_version: int
    chunk_index: int
    page_number: int | None
    page_end: int | None


class AgentState(TypedDict):
    execution_id: str
    user_message: str
    route: str
    route_reason: str
    smalltalk: str | None
    retrieval_query: str
    rewritten: bool
    evidence: list[EvidenceRef]
    readable_documents: int
    reranked: bool
    degraded: list[str]
    attempts: int
    validation_errors: list[str]
    raw_output: str
    verified: bool
    response: dict[str, Any] | None
    cache_status: str
    llm_calls: int
    prompt_tokens: int
    completion_tokens: int
    model_name: str
    answer_support: str
    assistant_message_id: str | None


def fresh_turn_state(execution_id: uuid.UUID, user_message: str) -> AgentState:
    """Every field set explicitly: a new turn never inherits anything from the last one."""
    return AgentState(
        execution_id=str(execution_id),
        user_message=user_message,
        route="",
        route_reason="",
        smalltalk=None,
        retrieval_query=user_message,
        rewritten=False,
        evidence=[],
        readable_documents=0,
        reranked=False,
        degraded=[],
        attempts=0,
        validation_errors=[],
        raw_output="",
        verified=False,
        response=None,
        cache_status="none",
        llm_calls=0,
        prompt_tokens=0,
        completion_tokens=0,
        model_name="",
        answer_support="none",
        assistant_message_id=None,
    )


SearchFn = Callable[[str], Awaitable[RetrievalResult]]
MemoryFn = Callable[[str], Awaitable[list[str]]]
VerifyFn = Callable[[DraftAnswer, Mapping[str, EvidenceItem]], Awaitable[VerificationResult]]
PersistFn = Callable[[dict[str, Any]], Awaitable[uuid.UUID]]
StatusFn = Callable[[str], Awaitable[None]]


@dataclass(slots=True)
class TurnContext:
    user_id: uuid.UUID
    settings: Settings
    gateway: LLMGateway
    search: SearchFn
    memories: MemoryFn
    verify: VerifyFn
    persist: PersistFn
    history: list[HistoryTurn] = field(default_factory=list[HistoryTurn])
    nonce: str = ""
    status: StatusFn | None = None
    # filled while the graph runs (not checkpointed)
    evidence: dict[str, EvidenceItem] = field(default_factory=dict[str, EvidenceItem])
    shown_labels: list[str] = field(default_factory=list[str])
    memory_texts: list[str] = field(default_factory=list[str])
    llm_response: Any = None  # the LLMResponse to remember once the answer is saved
    cacheable: bool = False
    started: float = field(default_factory=time.perf_counter)

    async def announce(self, stage: str) -> None:
        if self.status is not None:
            await self.status(stage)
