"""Generation evaluation with a REAL free hosted model (needs the user's own free API key).

    $env:REAL_LLM_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"   # example
    $env:REAL_LLM_API_KEY  = "<your free key>"
    $env:REAL_LLM_MODEL    = "gemini-2.5-flash"                                           # example
    $env:PYTHONPATH = "backend;."; uv run --no-sync python evaluation/generation/run_eval.py --delay 4

Runs the PRODUCTION agent graph (routing, retrieval with the production Docling chunks, prompt
construction, structured output, bounded repair, citation verification) over the synthetic corpus
of evaluation/chunking/corpus.py: every fact question plus hand-written unanswerable questions.
Persistence is a no-op and no database user is involved; everything else is the production code.

Measured (nothing is estimated; the report says when a metric could not be measured):
  json_first_try      answers that parsed, validated and verified without a repair call
  repaired / fallback share of questions that needed a repair / ended in the safe fallback
  citation_gold       a verified citation points at the chunk that contains the answer sentence
  answer_contains     the answer contains the key tokens of the fact (cheap correctness proxy)
  refusal_accuracy    unanswerable questions answered with insufficient_evidence (no invention)
  latency p50/p95, llm calls and tokens per question, provider errors (e.g. 429)
HONEST LIMITS: 36 + 10 synthetic questions, one model; free tiers rate-limit (the run paces itself
and records the errors instead of hiding them); results rank THIS setup, not general quality.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import time
import uuid
from pathlib import Path
from typing import Any

from pydantic import SecretStr
from redis.asyncio import Redis

from app.agents.citations import verify_draft
from app.agents.single import build_single_agent_graph, run_turn
from app.agents.state import TurnContext, fresh_turn_state
from app.core.config import get_settings
from app.core.exceptions import AppError
from app.ingestion.embedding import FastEmbedEmbedder
from app.llm.budget import QuotaGuard
from app.llm.cache import LLMGateway
from app.llm.openai_compat import OpenAICompatibleModel
from app.retrieval.models import RetrievalMode, RetrievalResult
from app.retrieval.rerank import FastEmbedReranker
from app.retrieval.retriever import Retriever
from evaluation.chunking.corpus import DOCS, all_facts
from evaluation.retrieval.run_eval import (
    USER_ID,
    doc_id,
    index_corpus,
    relevant_ids,
)

REPORTS = Path(__file__).resolve().parents[1] / "reports"

UNANSWERABLE = [
    "Who is the chief executive officer of the company?",
    "What is the company's phone number?",
    "How much did the company pay in dividends last year?",
    "Which cloud provider hosts the platform?",
    "What is the name of the security team lead?",
    "When was the company founded?",
    "What is the salary of a senior engineer?",
    "Which programming language is the API written in?",
    "How many offices does the company have in Asia?",
    "What colour is the company logo?",
]


def overlap_tokens(sentence: str) -> set[str]:
    stop = {"the", "a", "an", "of", "to", "in", "on", "is", "are", "by", "for", "and", "after"}
    return {
        t
        for t in "".join(c if c.isalnum() else " " for c in sentence.lower()).split()
        if t not in stop
    }


async def main_async(delay: float, limit: int | None, force: bool) -> None:
    configured = get_settings()  # LLM_* from the environment / .env
    base = os.environ.get("REAL_LLM_BASE_URL") or configured.llm_base_url
    key = os.environ.get("REAL_LLM_API_KEY") or configured.llm_api_key.get_secret_value()
    model = os.environ.get("REAL_LLM_MODEL") or configured.llm_model
    if not (base and key and model):
        raise SystemExit("set LLM_BASE_URL, LLM_API_KEY and LLM_MODEL in .env (or REAL_LLM_*)")
    fallbacks = [
        m for m in os.environ.get("REAL_LLM_FALLBACK_MODELS", "").split(",") if m.strip()
    ] or configured.llm_fallback_models
    settings = configured.model_copy(
        update={
            "qdrant_collection_prefix": "evalgen_",
            "pdf_pipeline": "docling",
            "llm_base_url": base,
            "llm_api_key": SecretStr(key),
            "llm_model": model,
            "llm_fallback_models": fallbacks,
            "llm_cache_enabled": False,  # measure the model, not the cache
            "rerank_timeout_s": 300.0,
            "retrieval_timeout_s": 300.0,
        }
    )
    embedder = FastEmbedEmbedder(settings)
    reranker = FastEmbedReranker(settings)
    chunks_by_doc = await asyncio.to_thread(index_corpus, settings, embedder)
    from qdrant_client import AsyncQdrantClient

    qdrant = AsyncQdrantClient(url=settings.qdrant_url, timeout=60)
    from concurrent.futures import ThreadPoolExecutor

    retriever = Retriever(qdrant, settings, embedder, reranker, ThreadPoolExecutor(max_workers=2))
    doc_ids = [doc_id(d.name) for d in DOCS]
    redis = Redis.from_url(settings.redis_url)  # pyright: ignore[reportUnknownMemberType]
    questions: list[tuple[str, str, Any]] = [("answerable", f.question, f) for f in all_facts()]
    questions += [("unanswerable", q, None) for q in UNANSWERABLE]
    if limit:
        questions = questions[:limit]
    quota = QuotaGuard(redis, settings)  # the evaluation shares the application's daily budget
    real = OpenAICompatibleModel(settings, quota=quota)
    gateway = LLMGateway(real, redis, settings)
    needed = len(questions) + 4  # about one call per question plus a few repairs/rewrites
    usage = (await quota.usage([model]))[0]
    print(
        f"quota: used today {usage.used_today} of {usage.daily_budget or 'unlimited'} "
        f"(remaining {usage.remaining}); this run needs about {needed}",
        flush=True,
    )
    if usage.remaining is not None and usage.remaining < needed and not force:
        raise SystemExit(
            f"only {usage.remaining} requests left today for {model}; run with --limit "
            f"{max(1, usage.remaining - 4)} or tomorrow, or pass --force"
        )
    graph = build_single_agent_graph(None, repair_budget=settings.llm_repair_attempts)

    async def search(query: str) -> RetrievalResult:
        result = await retriever.search(
            user_id=USER_ID,
            query=query,
            document_ids=doc_ids,
            mode=RetrievalMode.HYBRID_RERANK,
            top_k=settings.retrieval_top_k,
        )
        result.readable_documents = len(doc_ids)
        for chunk in result.chunks:
            chunk.document_name = chunk.hit.filename
        return result

    async def memories(_query: str) -> list[str]:
        return []

    async def verify(draft: Any, evidence: Any) -> Any:
        readable = {
            item.document_id: (item.document_name, item.document_version)
            for item in evidence.values()
        }
        return verify_draft(draft, evidence, readable)

    async def persist(_response: dict[str, Any]) -> uuid.UUID:
        return uuid.uuid4()

    rows: list[dict[str, Any]] = []
    for kind, question, fact in questions:
        ctx = TurnContext(
            user_id=USER_ID,
            settings=settings,
            gateway=gateway,
            search=search,
            memories=memories,
            verify=verify,
            persist=persist,
        )
        started = time.perf_counter()
        row: dict[str, Any] = {"kind": kind, "question": question}
        try:
            state = await run_turn(
                graph, ctx, fresh_turn_state(uuid.uuid4(), question), str(uuid.uuid4())
            )
            response = state["response"] or {}
            row.update(
                {
                    "attempts": state["attempts"],
                    "llm_calls": state["llm_calls"],
                    "insufficient": bool(response.get("insufficient_evidence")),
                    "degraded": state["degraded"],
                    "citations": len(response.get("citations", [])),
                    "answer": str(response.get("answer", ""))[:240],
                }
            )
            if fact is not None:
                gold = {
                    ref["chunk_id"]
                    for ref in state["evidence"]
                    if ref["chunk_id"] in relevant_ids_for(fact, chunks_by_doc)
                }
                cited = {c["chunk_id"] for c in response.get("citations", [])}
                row["citation_gold"] = bool(cited & gold)
                key_tokens = overlap_tokens(fact.sentence)
                row["answer_contains"] = len(
                    key_tokens & overlap_tokens(str(response.get("answer", "")))
                ) / max(1, len(key_tokens))
        except AppError as exc:
            row["error"] = exc.code
        row["latency_s"] = round(time.perf_counter() - started, 2)
        rows.append(row)
        print(
            json.dumps(
                {k: row[k] for k in ("kind", "latency_s") if k in row}
                | {"q": question[:50], "err": row.get("error")}
            ),
            flush=True,
        )
        await asyncio.sleep(delay)
    await qdrant.close()
    await real.aclose()
    await redis.aclose()
    write_report(rows, model)


def relevant_ids_for(fact: Any, chunks_by_doc: dict[str, list[Any]]) -> set[str]:
    from evaluation.retrieval.run_eval import Query

    return relevant_ids(
        Query("x", "natural", fact.question, fact.doc, fact.sentence), chunks_by_doc
    )


def write_report(rows: list[dict[str, Any]], model: str) -> None:
    ok = [r for r in rows if "error" not in r]
    answerable = [r for r in ok if r["kind"] == "answerable"]
    unanswerable = [r for r in ok if r["kind"] == "unanswerable"]

    def share(items: list[dict[str, Any]], pred: Any) -> float:
        return sum(1 for r in items if pred(r)) / len(items) if items else float("nan")

    latencies = sorted(r["latency_s"] for r in ok)
    metrics = {
        "model": model,
        "date": time.strftime("%Y-%m-%d"),
        "questions": len(rows),
        "provider_errors": {
            c: sum(1 for r in rows if r.get("error") == c)
            for c in {r["error"] for r in rows if "error" in r}
        },
        "json_first_try": share(answerable, lambda r: r["attempts"] == 1 and not r["insufficient"]),
        "repaired": share(ok, lambda r: r["attempts"] > 1),
        "fallback": share(ok, lambda r: "unverified_answer" in r["degraded"]),
        "citation_gold": share(answerable, lambda r: r.get("citation_gold")),
        "answer_contains_mean": statistics.mean([r["answer_contains"] for r in answerable])
        if answerable
        else float("nan"),
        "refusal_accuracy": share(unanswerable, lambda r: r["insufficient"]),
        "latency_p50_s": statistics.median(latencies) if latencies else float("nan"),
        "latency_p95_s": latencies[min(len(latencies) - 1, round(0.95 * (len(latencies) - 1)))]
        if latencies
        else float("nan"),
        "llm_calls_mean": statistics.mean([r["llm_calls"] for r in ok]) if ok else float("nan"),
    }
    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / "generation_eval.json").write_text(
        json.dumps({"metrics": metrics, "rows": rows}, indent=2), encoding="utf-8"
    )
    lines = [
        "# Generation evaluation (real free hosted model, synthetic corpus)",
        "",
        f"Model `{model}`, {metrics['date']}; {len(rows)} questions ({len(answerable)} answerable, {len(unanswerable)} unanswerable scored). Production agent graph; persistence disabled.",
        "",
        "| metric | value |",
        "|---|---|",
    ]
    for name, value in metrics.items():
        if name in ("model", "date", "questions"):
            continue
        lines.append(
            f"| {name} | {value if isinstance(value, dict) else (f'{value:.2f}' if isinstance(value, float) else value)} |"
        )
    lines += [
        "",
        "HONEST LIMITS: small synthetic set, one model, free-tier throttling (errors are counted above, not hidden).",
    ]
    (REPORTS / "generation_eval.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--delay", type=float, default=4.0, help="seconds between questions (free-tier pacing)"
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="only the first N questions (smoke run)"
    )
    parser.add_argument(
        "--force", action="store_true", help="run even if today's budget looks too small"
    )
    args = parser.parse_args()
    asyncio.run(main_async(args.delay, args.limit, args.force))


if __name__ == "__main__":
    main()
