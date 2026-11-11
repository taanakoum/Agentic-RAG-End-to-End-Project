"""The single-agent LangGraph: classify -> (answer without a model | retrieve -> generate ->
verify -> repair/fallback) -> finalize -> persist.

    START -> classify --smalltalk--> respond_smalltalk ---------------------------+
                      --clarify----> respond_clarify -----------------------------+
                      --kb---------> prepare_query -> retrieve -> evidence_gate   |
    evidence_gate --none--> respond_insufficient -------------------------------->+
                  --some--> generate -> check_output --ok-----------------------> +
                                          |  ^--repair (bounded)                  |
                                          +--exhausted--> respond_fallback ----->+
    finalize -> persist -> END

Every branch ends in `finalize`, which validates the user-facing `RAGResponse`; model output is
never returned unless it parsed, validated and passed server-side citation verification. The
number of model calls per turn is bounded: at most one rewrite plus (1 + LLM_REPAIR_ATTEMPTS)
answer calls; the graph has no other loop, and `recursion_limit` is a backstop.
"""

from __future__ import annotations

import asyncio
import functools
import time
from collections.abc import Mapping
from typing import Any

from langgraph.graph import END, START, StateGraph  # pyright: ignore[reportMissingTypeStubs]
from langgraph.runtime import Runtime
from pydantic import BaseModel, ConfigDict, Field

from app.agents import fallbacks
from app.agents.citations import EvidenceItem
from app.agents.context import (
    HistoryTurn,
    build_answer_messages,
    build_rewrite_messages,
    derive_nonce,
    repair_message,
)
from app.agents.routing import classify, needs_rewrite
from app.agents.schemas import (
    AgentMetadata,
    DraftAnswer,
    RAGResponse,
    draft_json_schema,
)
from app.agents.state import AgentState, EvidenceRef, TurnContext
from app.core.exceptions import (
    ModelProviderError,
    ModelRateLimitedError,
    ModelTimeoutError,
)
from app.core.logging import get_logger
from app.core.telemetry import span
from app.llm.base import ChatMessage, LLMRequest
from app.llm.structured import parse_model_json

log = get_logger(__name__)

SCHEMA_VERSION = "1"  # bump with the DraftAnswer contract (also part of the cache key)
RECURSION_LIMIT = 25
Ctx = Runtime[TurnContext]


class RewriteResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=400)


def _answer_dict(
    answer: str,
    *,
    insufficient: bool = False,
    clarification: bool = False,
    follow_ups: list[str] | None = None,
    citations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "answer": answer,
        "citations": citations or [],
        "insufficient_evidence": insufficient,
        "clarification_needed": clarification,
        "follow_up_questions": follow_ups or [],
    }


# ---------------------------------------------------------------------- nodes


async def classify_node(state: AgentState, runtime: Ctx) -> dict[str, Any]:
    routing = classify(state["user_message"])
    return {"route": routing.route, "route_reason": routing.reason, "smalltalk": routing.smalltalk}


def route_after_classify(state: AgentState) -> str:
    return {"smalltalk": "respond_smalltalk", "clarify": "respond_clarify"}.get(
        state["route"], "prepare_query"
    )


async def respond_smalltalk(state: AgentState, runtime: Ctx) -> dict[str, Any]:
    text = fallbacks.SMALLTALK.get(
        state["smalltalk"] or "greeting", fallbacks.SMALLTALK["greeting"]
    )
    return {"response": _answer_dict(text), "answer_support": "none"}


async def respond_clarify(state: AgentState, runtime: Ctx) -> dict[str, Any]:
    return {
        "response": _answer_dict(
            fallbacks.CLARIFY, clarification=True, follow_ups=fallbacks.CLARIFY_FOLLOW_UPS
        ),
        "answer_support": "none",
    }


async def prepare_query(state: AgentState, runtime: Ctx) -> dict[str, Any]:
    """Turn a short follow-up ("what about the second one?") into a standalone search query."""
    ctx = runtime.context
    message = state["user_message"]
    await ctx.announce("retrieving")
    if not needs_rewrite(message, has_history=bool(ctx.history)):
        return {"retrieval_query": message}
    calls = state["llm_calls"]
    spent_in, spent_out = state["prompt_tokens"], state["completion_tokens"]
    degraded = list(state["degraded"])
    try:
        response = await ctx.gateway.complete(
            LLMRequest(
                messages=tuple(build_rewrite_messages(message, ctx.history)),
                json_schema=RewriteResult.model_json_schema(),
                schema_name="rewrite",
                schema_version=SCHEMA_VERSION,
                user_id=ctx.user_id,
                max_tokens=120,
                temperature=0.0,
            )
        )
        calls += 1
        spent_in += 0 if response.cache == "hit" else response.usage.prompt_tokens
        spent_out += 0 if response.cache == "hit" else response.usage.completion_tokens
        parsed = parse_model_json(response.text, RewriteResult)
        if parsed.value is not None:
            await ctx.gateway.remember(response)
            return {
                "retrieval_query": parsed.value.query,
                "rewritten": True,
                "llm_calls": calls,
                "prompt_tokens": spent_in,
                "completion_tokens": spent_out,
            }
    except (ModelProviderError, ModelRateLimitedError, ModelTimeoutError):
        log.warning("query_rewrite_unavailable")
    # fallback: the previous user question plus the follow-up still carries the topic
    previous = next((t.content for t in reversed(ctx.history) if t.role == "user"), "")
    degraded.append("rewrite_fallback")
    return {
        "retrieval_query": f"{previous} {message}".strip()[:400],
        "rewritten": True,
        "llm_calls": calls,
        "prompt_tokens": spent_in,
        "completion_tokens": spent_out,
        "degraded": degraded,
    }


async def retrieve(state: AgentState, runtime: Ctx) -> dict[str, Any]:
    ctx = runtime.context
    query = state["retrieval_query"]
    degraded = list(state["degraded"])

    async def memories() -> list[str]:
        try:
            return await ctx.memories(query)
        except Exception as exc:  # memory is an enhancement: never fail the turn for it
            log.warning("memory_lookup_failed", error=type(exc).__name__)
            degraded.append("memory_unavailable")
            return []

    result, memory_texts = await asyncio.gather(ctx.search(query), memories())
    items: dict[str, EvidenceItem] = {}
    refs: list[EvidenceRef] = []
    for index, chunk in enumerate(result.chunks, start=1):
        hit = chunk.hit
        label = f"S{index}"
        items[label] = EvidenceItem(
            label=label,
            chunk_id=hit.chunk_id,
            document_id=hit.document_id,
            document_version=hit.document_version,
            chunk_index=hit.chunk_index,
            page_number=hit.page_number,
            page_end=hit.page_end,
            text=hit.text,
            document_name=chunk.document_name or hit.filename,
        )
        refs.append(
            EvidenceRef(
                label=label,
                chunk_id=hit.chunk_id,
                document_id=str(hit.document_id),
                document_version=hit.document_version,
                chunk_index=hit.chunk_index,
                page_number=hit.page_number,
                page_end=hit.page_end,
            )
        )
    ctx.evidence = items
    ctx.memory_texts = memory_texts
    degraded += [d for d in result.degraded if d not in degraded]
    return {
        "evidence": refs,
        "readable_documents": result.readable_documents,
        "reranked": any(c.rerank_score is not None for c in result.chunks),
        "degraded": degraded,
    }


def route_after_retrieval(state: AgentState) -> str:
    return "generate" if state["evidence"] else "respond_insufficient"


async def respond_insufficient(state: AgentState, runtime: Ctx) -> dict[str, Any]:
    text = fallbacks.NO_EVIDENCE if state["readable_documents"] else fallbacks.NO_DOCUMENTS
    return {
        "response": _answer_dict(text, insufficient=True),
        "answer_support": "not_found",
    }


async def generate(state: AgentState, runtime: Ctx) -> dict[str, Any]:
    ctx = runtime.context
    s = ctx.settings
    repairing = state["attempts"] > 0
    await ctx.announce("repairing" if repairing else "generating")
    question = state["user_message"]
    if state["rewritten"]:
        question = f"{question}\n(Understood as: {state['retrieval_query']})"
    ordered = [ctx.evidence[ref["label"]] for ref in state["evidence"]]
    ctx.nonce = derive_nonce(
        s.jwt_secret.get_secret_value(),
        str(ctx.user_id),
        question,
        *[item.chunk_id for item in ordered],
    )
    messages, shown = build_answer_messages(
        question=question,
        evidence=ordered,
        memories=ctx.memory_texts,
        history=ctx.history,
        nonce=ctx.nonce,
        max_chars=s.llm_context_max_chars,
    )
    ctx.shown_labels = [item.label for item in shown]
    if repairing:
        messages.append(ChatMessage("assistant", state["raw_output"][:4000]))
        messages.append(repair_message(state["validation_errors"], ctx.shown_labels))
    response = await ctx.gateway.complete(
        LLMRequest(
            messages=tuple(messages),
            json_schema=draft_json_schema(),
            schema_name="rag_answer",
            schema_version=SCHEMA_VERSION,
            user_id=ctx.user_id,
        )
    )
    ctx.llm_response = response
    return {
        "raw_output": response.text,
        "attempts": state["attempts"] + 1,
        "llm_calls": state["llm_calls"] + 1,
        "prompt_tokens": state["prompt_tokens"]
        + (0 if response.cache == "hit" else response.usage.prompt_tokens),
        "completion_tokens": state["completion_tokens"]
        + (0 if response.cache == "hit" else response.usage.completion_tokens),
        "cache_status": response.cache,
        "model_name": response.model,
    }


async def check_output(state: AgentState, runtime: Ctx) -> dict[str, Any]:
    ctx = runtime.context
    await ctx.announce("verifying")
    parsed = parse_model_json(state["raw_output"], DraftAnswer)
    errors: list[str] = list(parsed.errors)
    if parsed.value is not None:
        shown: Mapping[str, EvidenceItem] = {
            label: ctx.evidence[label] for label in ctx.shown_labels
        }
        result = await ctx.verify(parsed.value, shown)
        if result.ok:
            draft = parsed.value
            ctx.cacheable = not (draft.insufficient_evidence or draft.clarification_needed)
            return {
                "verified": True,
                "validation_errors": [],
                "answer_support": draft.support if not draft.insufficient_evidence else "not_found",
                "response": _answer_dict(
                    result.answer,
                    insufficient=draft.insufficient_evidence,
                    clarification=draft.clarification_needed,
                    follow_ups=draft.follow_up_questions,
                    citations=[c.model_dump() for c in result.citations],
                ),
            }
        errors = [f"citations: {result.problem}"] + [
            f"citation: {d}" for d in sorted(set(result.dropped))
        ]
    llm_response = ctx.llm_response
    if llm_response is not None:
        await ctx.gateway.discard(llm_response)  # a cached answer that fails today is not reused
    return {"verified": False, "validation_errors": errors}


def route_after_check(state: AgentState, repair_budget: int) -> str:
    if state["verified"]:
        return "finalize"
    return "generate" if state["attempts"] < 1 + repair_budget else "respond_fallback"


async def respond_fallback(state: AgentState, runtime: Ctx) -> dict[str, Any]:
    degraded = [*state["degraded"], "unverified_answer"]
    return {
        "response": _answer_dict(fallbacks.UNVERIFIED, insufficient=True),
        "answer_support": "not_found",
        "degraded": degraded,
    }


async def finalize(state: AgentState, runtime: Ctx) -> dict[str, Any]:
    ctx = runtime.context
    body = dict(state["response"] or _answer_dict(fallbacks.UNVERIFIED, insufficient=True))
    kb = state["route"] == "kb"
    metadata = AgentMetadata(
        execution_mode="single",
        model_name=state["model_name"] or ctx.gateway.model_name if state["llm_calls"] else "none",
        retrieval_used=kb,
        reranking_used=state["reranked"],
        answer_support=state["answer_support"],  # pyright: ignore[reportArgumentType]
        cache_status=state["cache_status"],  # pyright: ignore[reportArgumentType]
        llm_calls=state["llm_calls"],
        prompt_tokens=state["prompt_tokens"],
        completion_tokens=state["completion_tokens"],
        latency_ms=int((time.perf_counter() - ctx.started) * 1000),
        degraded=state["degraded"],
        route=state["route"],
    )
    response = RAGResponse(**body, metadata=metadata)  # pyright: ignore[reportArgumentType]
    return {"response": response.model_dump(mode="json")}


async def persist(state: AgentState, runtime: Ctx) -> dict[str, Any]:
    ctx = runtime.context
    await ctx.announce("saving")
    message_id = await ctx.persist(state["response"] or {})
    return {"assistant_message_id": str(message_id)}


# ---------------------------------------------------------------------- graph


_NODE_ATTRS = (
    "route",
    "route_reason",
    "attempts",
    "verified",
    "llm_calls",
    "prompt_tokens",
    "completion_tokens",
    "cache_status",
    "readable_documents",
    "reranked",
    "answer_support",
    "rewritten",
    "degraded",
    "validation_errors",
)


def _traced(name: str, node: Any) -> Any:
    """One span per graph node, with counts/flags from its result (never text)."""

    @functools.wraps(node)
    async def wrapper(state: AgentState, runtime: Ctx) -> dict[str, Any]:
        with span(f"agent.{name}") as current:
            delta: dict[str, Any] = await node(state, runtime)
            attrs = {f"agent.{k}": delta[k] for k in _NODE_ATTRS if k in delta}
            if "evidence" in delta:
                attrs["agent.evidence_count"] = len(delta["evidence"])
            for key, value in attrs.items():
                current.set_attribute(key, value)
            return delta

    return wrapper


def build_single_agent_graph(checkpointer: Any, *, repair_budget: int) -> Any:
    raw: Any = StateGraph(AgentState, context_schema=TurnContext)

    class _Traced:
        """Registers every node through `_traced` without repeating it eleven times."""

        def __getattr__(self, attr: str) -> Any:
            return getattr(raw, attr)

        def add_node(self, name: str, node: Any) -> None:
            raw.add_node(name, _traced(name, node))

    graph: Any = _Traced()
    graph.add_node("classify", classify_node)
    graph.add_node("respond_smalltalk", respond_smalltalk)
    graph.add_node("respond_clarify", respond_clarify)
    graph.add_node("prepare_query", prepare_query)
    graph.add_node("retrieve", retrieve)
    graph.add_node("respond_insufficient", respond_insufficient)
    graph.add_node("generate", generate)
    graph.add_node("check_output", check_output)
    graph.add_node("respond_fallback", respond_fallback)
    graph.add_node("finalize", finalize)
    graph.add_node("persist", persist)

    graph.add_edge(START, "classify")
    graph.add_conditional_edges(
        "classify",
        route_after_classify,
        ["respond_smalltalk", "respond_clarify", "prepare_query"],
    )
    for terminal in (
        "respond_smalltalk",
        "respond_clarify",
        "respond_insufficient",
        "respond_fallback",
    ):
        graph.add_edge(terminal, "finalize")
    graph.add_edge("prepare_query", "retrieve")
    graph.add_conditional_edges(
        "retrieve", route_after_retrieval, ["generate", "respond_insufficient"]
    )
    graph.add_edge("generate", "check_output")

    def after_check(state: AgentState) -> str:
        return route_after_check(state, repair_budget)

    graph.add_conditional_edges(
        "check_output", after_check, ["finalize", "generate", "respond_fallback"]
    )
    graph.add_edge("finalize", "persist")
    graph.add_edge("persist", END)
    return raw.compile(checkpointer=checkpointer)


async def run_turn(graph: Any, ctx: TurnContext, state: AgentState, thread_id: str) -> AgentState:
    """Run one turn. The thread id is the conversation's LangGraph thread (never client-visible)."""
    result: AgentState = await graph.ainvoke(
        state,
        {"configurable": {"thread_id": thread_id}, "recursion_limit": RECURSION_LIMIT},
        context=ctx,
        durability="exit",
    )
    return result


def history_from(pairs: list[tuple[str, str]]) -> list[HistoryTurn]:
    return [HistoryTurn(role, content) for role, content in pairs]
