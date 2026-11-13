"""Re-ranking benchmark: where does the time go, and what can be cut without losing quality (KI-023).

Reproduce (needs the dev Qdrant; downloads the real models on first use, ~1.7 GB in total):

    $env:PYTHONPATH = "backend;."
    uv run --no-sync python evaluation/retrieval/real_docs.py --download
    uv run --no-sync python evaluation/retrieval/rerank_bench.py

Method
  1. Index the synthetic corpus (evaluation/chunking/corpus.py, natural questions) and the real
     documents (evaluation/datasets/real_docs_v1.json) into one throw-away collection, one user each.
  2. With the PRODUCTION Retriever in hybrid mode, take the top-8 fused candidates per question: this
     is exactly the list the production pipeline hands to the cross-encoder.
  3. QUALITY: a cross-encoder scores every (question, candidate) pair independently, so the scores of
     the 8 candidates are computed once per (model, input length) and every other knob is replayed
     on them exactly as `Retriever._rerank_impl` does (input = `section_path + text` cut at the
     length, the first N candidates are re-ordered, the rest follow in fused order): number of
     candidates (5 / 8) and the adaptive skip rule (`app.retrieval.skip`, the production function).
  4. LATENCY: measured separately on real `score()` calls (warm model, 8 questions spread over both
     datasets, 3 repeats, the first call of every model is discarded), one factor at a time around
     the current default: model, input length, ONNX threads, candidates.
  5. Rule for new defaults (docs/plans/REMAINING_ISSUES_PLAN.md item 12): on BOTH datasets hit@1 and
     MRR within ONE question of the current default; among those the lowest median latency.

HONEST LIMITS: 64 questions on 8 documents; one laptop (4 physical / 8 logical cores) with the
benchmark process alone; ONNX Runtime CPU only; latency is per `score()` call (it excludes the
queue wait on the inference pool and the vector search); the synthetic corpus is easy for lexical
signals, the real documents are two public documents with questions written by the project author.
"""

from __future__ import annotations

import asyncio
import json
import platform
import statistics
import sys
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from qdrant_client import AsyncQdrantClient, QdrantClient

from app.core.config import Settings, get_settings
from app.ingestion.embedding import FastEmbedEmbedder
from app.ingestion.indexing import collection_name
from app.retrieval.models import RankedChunk, RetrievalMode
from app.retrieval.rerank import FastEmbedReranker
from app.retrieval.retriever import Retriever
from app.retrieval.skip import should_skip_rerank
from evaluation.retrieval import real_docs, run_eval

REPORTS = Path(__file__).resolve().parents[1] / "reports"
CACHE = Path(__file__).resolve().parents[1] / ".cache" / "rerank_scores.json"
MODELS = (
    "BAAI/bge-reranker-v2-m3-int8",  # the current default
    "jinaai/jina-reranker-v2-base-multilingual",
    "Xenova/ms-marco-MiniLM-L-12-v2",
)
CURRENT = MODELS[0]
CHARS = (2000, 1200, 800)
CANDIDATES = (5, 8)
THREADS = (0, 4, 8)  # 0 = library default
MARGINS = (0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.8)
POOL = 8
LATENCY_QUESTIONS = 8
REPEATS = 3


@dataclass
class Cand:
    chunk_id: str
    source: str  # section_path + text exactly as the production reranker builds it
    dense_rank: int | None
    sparse_rank: int | None
    fused: float


@dataclass
class Item:
    dataset: str
    qid: str
    text: str
    relevant: set[str]
    cands: list[Cand]

    @property
    def key(self) -> str:
        return f"{self.dataset}/{self.qid}"


def passage(c: Cand, limit: int) -> str:
    return c.source[:limit]


def as_ranked(cands: Sequence[Cand]) -> list[RankedChunk]:
    """Minimal RankedChunk list for the production skip rule."""
    from app.retrieval.models import Hit

    out: list[RankedChunk] = []
    for c in cands:
        hit = Hit(c.chunk_id, run_eval.doc_id("x"), 1, 0, "", None, None, "", None, "")
        out.append(
            RankedChunk(
                hit=hit, dense_rank=c.dense_rank, sparse_rank=c.sparse_rank, fused_score=c.fused
            )
        )
    return out


# ------------------------------------------------------------------ candidates


async def collect(
    settings: Settings,
    embedder: FastEmbedEmbedder,
    reranker: FastEmbedReranker,
    dataset: str,
    user_id: Any,
    doc_ids: list[Any],
    queries: Sequence[tuple[str, str, set[str]]],
) -> list[Item]:
    executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="bench")
    qdrant = AsyncQdrantClient(url=settings.qdrant_url, timeout=60)
    retriever = Retriever(qdrant, settings, embedder, reranker, executor)
    items: list[Item] = []
    try:
        for qid, text, relevant in queries:
            result = await retriever.search(
                user_id=user_id,
                query=text,
                document_ids=doc_ids,
                mode=RetrievalMode.HYBRID,
                top_k=10,
            )
            cands = [
                Cand(
                    c.hit.chunk_id,
                    (f"{c.hit.section_path}\n{c.hit.text}" if c.hit.section_path else c.hit.text),
                    c.dense_rank,
                    c.sparse_rank,
                    float(c.fused_score or 0.0),
                )
                for c in result.chunks
            ]
            items.append(Item(dataset, qid, text, relevant, cands))
    finally:
        await qdrant.close()
        executor.shutdown(wait=True)
    return items


async def build_items(base: Settings, embedder: FastEmbedEmbedder) -> list[Item]:
    reranker = FastEmbedReranker(base)
    chunks = await asyncio.to_thread(run_eval.index_corpus, base, embedder)
    _, docs, questions = real_docs.load_dataset()
    real_docs.require_files(docs)
    real_chunks = await asyncio.to_thread(real_docs.parse_documents, base, docs)
    await asyncio.to_thread(
        real_docs.index_documents, base, embedder, docs, real_chunks, reset=False
    )

    synth_queries = [q for q in run_eval.build_queries() if q.style == "natural"]
    synth = [
        (q.qid, q.text, run_eval.relevant_ids(q, chunks))
        for q in synth_queries
        if run_eval.relevant_ids(q, chunks)
    ]
    real = [
        (q.qid, q.question, real_docs.relevant_ids(q, real_chunks))
        for q in questions
        if real_docs.relevant_ids(q, real_chunks)
    ]
    items = await collect(
        base,
        embedder,
        reranker,
        "synthetic",
        run_eval.USER_ID,
        [run_eval.doc_id(d.name) for d in run_eval.DOCS],
        synth,
    )
    items += await collect(
        base,
        embedder,
        reranker,
        "real",
        real_docs.USER_ID,
        [real_docs.doc_id(d.name) for d in docs],
        real,
    )
    return items


# ------------------------------------------------------------------ scores (quality)


def load_scores() -> dict[str, list[float]]:
    try:
        return dict(json.loads(CACHE.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return {}


def save_scores(scores: dict[str, list[float]]) -> None:
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(scores), encoding="utf-8")


def compute_scores(base: Settings, items: Sequence[Item]) -> dict[str, list[float]]:
    """Scores of the first POOL candidates of every question for every (model, length)."""
    scores = load_scores()
    for model in MODELS:
        reranker = FastEmbedReranker(base.model_copy(update={"reranker_model": model}))
        reranker.score("warm up", ["warm up"])
        for chars in CHARS:
            started = time.perf_counter()
            todo = 0
            for item in items:
                key = f"{model}|{chars}|{item.key}"
                if key in scores:
                    continue
                todo += 1
                passages = [passage(c, chars) for c in item.cands[:POOL]]
                scores[key] = reranker.score(item.text, passages)
            save_scores(scores)
            print(
                f"scores {model} @{chars}: {todo} questions in {time.perf_counter() - started:.0f}s",
                flush=True,
            )
    return scores


def ranking(item: Item, scores: list[float] | None, candidates: int) -> list[str]:
    head, tail = item.cands[:candidates], item.cands[candidates:]
    if scores is not None:
        order = sorted(range(len(head)), key=lambda i: -scores[i])  # stable like production
        head = [head[i] for i in order]
    return [c.chunk_id for c in head + tail]


def quality(
    items: Sequence[Item],
    scores: dict[str, list[float]],
    model: str,
    chars: int,
    candidates: int,
    margin: float,
) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for dataset in ("synthetic", "real"):
        subset = [i for i in items if i.dataset == dataset]
        top1 = mrr = skipped = 0.0
        top3 = 0.0
        for item in subset:
            skip = should_skip_rerank(as_ranked(item.cands[:candidates]), margin)
            skipped += skip
            ranked = ranking(
                item, None if skip else scores[f"{model}|{chars}|{item.key}"], candidates
            )
            top1 += ranked[0] in item.relevant
            top3 += any(r in item.relevant for r in ranked[:3])
            for position, cid in enumerate(ranked, start=1):
                if cid in item.relevant:
                    mrr += 1.0 / position
                    break
        out[dataset] = {
            "n": float(len(subset)),
            "top1": top1,
            "top3": top3,
            "mrr_sum": mrr,
            "skipped": skipped,
        }
    return out


# ------------------------------------------------------------------ latency


def latency(
    base: Settings, items: Sequence[Item], model: str, chars: int, threads: int, cands: int
) -> dict[str, float]:
    reranker = FastEmbedReranker(
        base.model_copy(update={"reranker_model": model, "rerank_threads": threads})
    )
    step = max(1, len(items) // LATENCY_QUESTIONS)
    sample = list(items[::step])[:LATENCY_QUESTIONS]
    reranker.score("warm up", ["warm up"] * cands)  # load + first-call cost, discarded
    times: list[float] = []
    for _ in range(REPEATS):
        for item in sample:
            passages = [passage(c, chars) for c in item.cands[:cands]]
            started = time.perf_counter()
            reranker.score(item.text, passages)
            times.append((time.perf_counter() - started) * 1000)
    ordered = sorted(times)
    return {
        "p50_ms": statistics.median(times),
        "p95_ms": ordered[min(len(ordered) - 1, round(0.95 * (len(ordered) - 1)))],
        "samples": float(len(times)),
    }


# ------------------------------------------------------------------ report


def versions() -> dict[str, str]:
    import importlib.metadata as md

    out: dict[str, str] = {}
    for name in ("fastembed", "onnxruntime", "numpy"):
        try:
            out[name] = md.version(name)
        except md.PackageNotFoundError:
            out[name] = "?"
    return out


def main() -> None:
    asyncio.run(main_async())


async def main_async() -> None:
    base = get_settings().model_copy(
        update={
            "qdrant_collection_prefix": "eval_",
            "pdf_pipeline": "docling",
            "rerank_timeout_s": 600.0,
            "retrieval_timeout_s": 600.0,
            "retrieval_cache_enabled": False,
        }
    )
    embedder = FastEmbedEmbedder(base)
    items = await build_items(base, embedder)
    print(f"{len(items)} questions with candidates", flush=True)

    scores = await asyncio.to_thread(compute_scores, base, items)

    grid: list[dict[str, Any]] = []
    for model in MODELS:
        for chars in CHARS:
            for cands in CANDIDATES:
                grid.append(
                    {
                        "model": model,
                        "chars": chars,
                        "candidates": cands,
                        "margin": 0.0,
                        "quality": quality(items, scores, model, chars, cands, 0.0),
                    }
                )
    baseline = {
        "model": "(no rerank)",
        "chars": 0,
        "candidates": 0,
        "margin": 0.0,
        "quality": _fused_only(items),
    }
    skip_rows = [
        {
            "model": CURRENT,
            "chars": 2000,
            "candidates": c,
            "margin": m,
            "quality": quality(items, scores, CURRENT, 2000, c, m),
        }
        for c in CANDIDATES
        for m in MARGINS
    ]

    # latency, one factor at a time around the default
    base_cfg = (CURRENT, 2000, 0, 5)
    configs: list[tuple[str, int, int, int]] = [base_cfg]
    configs += [(m, 2000, 0, 5) for m in MODELS[1:]]
    configs += [(CURRENT, c, 0, 5) for c in CHARS[1:]]
    configs += [(CURRENT, 2000, t, 5) for t in THREADS[1:]]
    configs += [(CURRENT, 2000, 0, 8)]
    lat_rows: list[dict[str, Any]] = []
    for model, chars, threads, cands in configs:
        result = await asyncio.to_thread(latency, base, items, model, chars, threads, cands)
        lat_rows.append(
            {"model": model, "chars": chars, "threads": threads, "candidates": cands, **result}
        )
        print(
            f"latency {model} chars={chars} threads={threads} cands={cands}: "
            f"p50 {result['p50_ms']:.0f} ms p95 {result['p95_ms']:.0f} ms",
            flush=True,
        )

    client = QdrantClient(url=base.qdrant_url, timeout=30)
    try:
        client.delete_collection(collection_name(base))
    finally:
        client.close()

    meta = {
        "date": time.strftime("%Y-%m-%d"),
        "hardware": f"{platform.processor() or platform.machine()}, {platform.system()}, CPU only",
        "versions": versions(),
        "questions": {d: sum(1 for i in items if i.dataset == d) for d in ("synthetic", "real")},
        "pool": POOL,
        "latency_questions": LATENCY_QUESTIONS,
        "repeats": REPEATS,
    }
    payload = {
        "meta": meta,
        "baseline_fused_only": baseline,
        "quality_grid": grid,
        "skip": skip_rows,
        "latency": lat_rows,
    }
    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / "rerank_bench.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    write_markdown(payload)
    print("report written to evaluation/reports/rerank_bench.*", flush=True)


def _fused_only(items: Sequence[Item]) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for dataset in ("synthetic", "real"):
        subset = [i for i in items if i.dataset == dataset]
        top1 = top3 = mrr = 0.0
        for item in subset:
            ranked = [c.chunk_id for c in item.cands]
            top1 += ranked[0] in item.relevant
            top3 += any(r in item.relevant for r in ranked[:3])
            for position, cid in enumerate(ranked, start=1):
                if cid in item.relevant:
                    mrr += 1.0 / position
                    break
        out[dataset] = {
            "n": float(len(subset)),
            "top1": top1,
            "top3": top3,
            "mrr_sum": mrr,
            "skipped": 0.0,
        }
    return out


def cell(q: dict[str, dict[str, float]], dataset: str) -> str:
    d = q[dataset]
    n = d["n"]
    return f"{int(d['top1'])}/{int(n)} | {int(d['top3'])}/{int(n)} | {d['mrr_sum'] / n:.3f}"


def write_markdown(p: dict[str, Any]) -> None:
    meta = p["meta"]
    lines = [
        "# Re-ranking benchmark (KI-023)",
        "",
        f"Date {meta['date']}; {meta['hardware']}; {meta['versions']}. Questions: "
        f"synthetic {meta['questions']['synthetic']} (natural style), real documents "
        f"{meta['questions']['real']}. Candidates come from the production hybrid retriever "
        f"(top-{meta['pool']} fused); latency is `score()` time on {meta['latency_questions']} "
        f"questions x {meta['repeats']} repeats after one discarded warm-up call.",
        "",
        "HONEST LIMITS: small question sets (one question = 2.8 points on the synthetic set, 3.6 on "
        "the real one); one laptop CPU with nothing else running; latency excludes queueing and the "
        "vector search; the quality of the real set depends on one author's questions.",
        "",
        "## Quality (hit@1 | hit@3 | MRR), every knob replayed on the same candidates",
        "",
        "| model | chars | candidates | synthetic hit@1 | hit@3 | MRR | real hit@1 | hit@3 | MRR |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    b = p["baseline_fused_only"]["quality"]
    lines.append(
        f"| (no rerank, fused order) | - | - | {cell(b, 'synthetic')} | {cell(b, 'real')} |"
    )
    for row in p["quality_grid"]:
        q = row["quality"]
        lines.append(
            f"| {row['model']} | {row['chars']} | {row['candidates']} | {cell(q, 'synthetic')} | {cell(q, 'real')} |"
        )
    lines += [
        "",
        "## Adaptive skip (current model, 2000 chars): skipped = questions that did not call the cross-encoder",
        "",
        "| candidates | margin | synthetic hit@1 | hit@3 | MRR | skipped | real hit@1 | hit@3 | MRR | skipped |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in p["skip"]:
        q = row["quality"]
        lines.append(
            f"| {row['candidates']} | {row['margin']} | {cell(q, 'synthetic')} | "
            f"{int(q['synthetic']['skipped'])}/{int(q['synthetic']['n'])} | {cell(q, 'real')} | "
            f"{int(q['real']['skipped'])}/{int(q['real']['n'])} |"
        )
    lines += [
        "",
        "## Latency of one `score()` call (one factor at a time around the default)",
        "",
        "| model | chars | ONNX threads | candidates | p50 ms | p95 ms |",
        "|---|---|---|---|---|---|",
    ]
    for r in p["latency"]:
        threads = "default" if r["threads"] == 0 else r["threads"]
        lines.append(
            f"| {r['model']} | {r['chars']} | {threads} | {r['candidates']} | {r['p50_ms']:.0f} | {r['p95_ms']:.0f} |"
        )
    (REPORTS / "rerank_bench.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    if "--markdown-only" in sys.argv:
        write_markdown(json.loads((REPORTS / "rerank_bench.json").read_text(encoding="utf-8")))
    else:
        main()
