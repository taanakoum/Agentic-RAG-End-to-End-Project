"""Retrieval evaluation: dense vs sparse vs hybrid (RRF) vs hybrid + cross-encoder rerank.

Reproduce (needs the dev Qdrant on 127.0.0.1:6333; downloads/uses the real open-source models):

    $env:PYTHONPATH = "backend;."; uv run --no-sync python evaluation/retrieval/run_eval.py

What it does
  1. Renders the synthetic corpus of evaluation/chunking/corpus.py to PDFs (6 documents).
  2. Parses and chunks them with the PRODUCTION pipeline (Docling HybridChunker, sandboxed).
  3. Embeds with the real models (BAAI/bge-small-en-v1.5 + Qdrant/bm25), indexes into a throw-away
     Qdrant collection (prefix `eval_`), as ONE user's library.
  4. Runs every configuration through the PRODUCTION Retriever (authorization filter, RRF,
     cross-encoder Xenova/ms-marco-MiniLM-L-6-v2) and scores it against the ground truth.

Ground truth is derived, not invented: a chunk is relevant to a question iff its text contains the
answer-bearing sentence of that question (whitespace/hyphen-insensitive).

HONEST LIMITS: the corpus is small (6 documents, 43 pages, 36 questions) and SYNTHETIC. Each
answer sentence omits the words that name its topic (they appear only in the section heading),
and every document contains distractor sections with the same sentence shape, so the questions
are not trivially lexical. Results rank the configurations on THIS data; they are not a claim
about quality on arbitrary real documents.
"""

from __future__ import annotations

import asyncio
import json
import statistics
import sys
import time
import uuid
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from qdrant_client import AsyncQdrantClient, QdrantClient

from app.core.config import Settings, get_settings
from app.ingestion.chunking import Chunk
from app.ingestion.embedding import FastEmbedEmbedder
from app.ingestion.indexing import collection_name, ensure_collection, point_id, upsert_chunks
from app.ingestion.parsing import SandboxedPdfParser, parse_bytes
from app.retrieval.models import RetrievalMode
from app.retrieval.rerank import FastEmbedReranker
from app.retrieval.retriever import Retriever
from evaluation.chunking.corpus import DOCS, Fact, build_pages, render_pdf
from evaluation.retrieval.metrics import summarize

REPORTS = Path(__file__).resolve().parents[1] / "reports"
DATASET_VERSION = "retrieval-synth-v1"
KS = (1, 3, 5, 10)
USER_ID = uuid.uuid5(uuid.NAMESPACE_URL, "retrieval-eval-user")
BGE_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "


CACHE = Path(__file__).resolve().parents[1] / ".cache" / "retrieval_results.json"


def _cache_key(settings: Settings) -> str:
    return f"{DATASET_VERSION}|{settings.embedding_model}|{settings.sparse_model}|{len(CONFIGS)}"


def load_cache(settings: Settings) -> dict[str, Any]:
    try:
        data = json.loads(CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if data.get("key") != _cache_key(settings):
        return {}
    return dict(data.get("results", {}))


def save_cache(settings: Settings, results: dict[str, Any]) -> None:
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(
        json.dumps({"key": _cache_key(settings), "results": results}), encoding="utf-8"
    )


def squash(text: str) -> str:
    """Whitespace- and hyphenation-insensitive form used to find the answer sentence."""
    return "".join(ch for ch in text.lower() if ch.isalnum())


@dataclass(frozen=True)
class Query:
    qid: str
    style: str  # "natural" | "heading_keywords"
    text: str
    doc: str
    sentence: str


def build_queries() -> list[Query]:
    queries: list[Query] = []
    for index, fact in enumerate(_facts()):
        queries.append(Query(f"q{index:02d}", "natural", fact.question, fact.doc, fact.sentence))
        # AUTO-DERIVED keyword-style variant: the section heading, as someone might type it
        queries.append(
            Query(
                f"q{index:02d}h", "heading_keywords", fact.heading.lower(), fact.doc, fact.sentence
            )
        )
    return queries


def _facts() -> list[Fact]:
    return [f for d in DOCS for f in d.facts]


@dataclass(frozen=True)
class Config:
    name: str
    mode: RetrievalMode
    overrides: dict[str, Any] = field(default_factory=dict[str, Any])


CONFIGS = (
    Config("dense", RetrievalMode.DENSE),
    Config("sparse (BM25)", RetrievalMode.SPARSE),
    Config("hybrid (RRF k=60)", RetrievalMode.HYBRID),
    Config(
        "hybrid + rerank (DEFAULT: bge-reranker-v2-m3 int8, 5 candidates)",
        RetrievalMode.HYBRID_RERANK,
        {"reranker_model": "BAAI/bge-reranker-v2-m3-int8", "rerank_candidates": 5},
    ),
    # ablations
    Config(
        "dense + BGE query instruction",
        RetrievalMode.DENSE,
        {"dense_query_prefix": BGE_QUERY_PREFIX},
    ),
    Config("hybrid (RRF k=10)", RetrievalMode.HYBRID, {"rrf_k": 10}),
    Config(
        "hybrid + rerank: ms-marco-MiniLM-L-6, 30 candidates",
        RetrievalMode.HYBRID_RERANK,
        {"reranker_model": "Xenova/ms-marco-MiniLM-L-6-v2", "rerank_candidates": 30},
    ),
    Config(
        "hybrid + rerank: ms-marco-MiniLM-L-6, 10 candidates",
        RetrievalMode.HYBRID_RERANK,
        {"reranker_model": "Xenova/ms-marco-MiniLM-L-6-v2", "rerank_candidates": 10},
    ),
    Config(
        "hybrid + rerank: ms-marco-MiniLM-L-12, 30 candidates",
        RetrievalMode.HYBRID_RERANK,
        {"reranker_model": "Xenova/ms-marco-MiniLM-L-12-v2", "rerank_candidates": 30},
    ),
    Config(
        "hybrid + rerank: jina-reranker-v1-turbo-en, 30 candidates",
        RetrievalMode.HYBRID_RERANK,
        {"reranker_model": "jinaai/jina-reranker-v1-turbo-en", "rerank_candidates": 30},
    ),
    Config(
        "hybrid + rerank: bge-reranker-v2-m3 (int8), 30 candidates",
        RetrievalMode.HYBRID_RERANK,
        {"reranker_model": "BAAI/bge-reranker-v2-m3-int8", "rerank_candidates": 30},
    ),
    Config(
        "hybrid + rerank: bge-reranker-v2-m3 (int8), 10 candidates",
        RetrievalMode.HYBRID_RERANK,
        {"reranker_model": "BAAI/bge-reranker-v2-m3-int8", "rerank_candidates": 10},
    ),
)


def index_corpus(settings: Settings, embedder: FastEmbedEmbedder) -> dict[str, list[Chunk]]:
    """Parse with the production pipeline and index; returns chunks per document name."""
    parser = SandboxedPdfParser(settings)
    client = QdrantClient(url=settings.qdrant_url, timeout=60)
    chunks_by_doc: dict[str, list[Chunk]] = {}
    try:
        if client.collection_exists(collection_name(settings)):
            client.delete_collection(collection_name(settings))
        ensure_collection(client, settings)
        for doc in DOCS:
            pages = build_pages(doc)
            parsed = parse_bytes(
                parser,
                render_pdf(pages),
                title=doc.title,
                page_count=len(pages),
                source_format="pdf",
            )
            texts = [c.embed_text or c.text for c in parsed.chunks]
            upsert_chunks(
                client,
                settings,
                user_id=USER_ID,
                document_id=doc_id(doc.name),
                version=1,
                filename=f"{doc.name}.pdf",
                chunks=parsed.chunks,
                dense=embedder.embed_dense(texts),
                sparse=embedder.embed_sparse(texts),
                embedding_model=embedder.model_name,
            )
            chunks_by_doc[doc.name] = parsed.chunks
            print(
                f"indexed {doc.name}: {len(parsed.chunks)} chunks via {parsed.parser}", flush=True
            )
    finally:
        parser.close()
        client.close()
    return chunks_by_doc


def doc_id(name: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"retrieval-eval-doc/{name}")


def relevant_ids(query: Query, chunks_by_doc: dict[str, list[Chunk]]) -> set[str]:
    key = squash(query.sentence)
    return {
        point_id(doc_id(query.doc), 1, c.index)
        for c in chunks_by_doc[query.doc]
        if key in squash(c.text)
    }


def pct(values: Sequence[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(q * (len(ordered) - 1)))]


async def run_config(
    config: Config,
    base: Settings,
    embedder: FastEmbedEmbedder,
    reranker: FastEmbedReranker,
    queries: list[Query],
    truth: dict[str, set[str]],
    texts: dict[str, str],
) -> dict[str, Any]:
    settings = base.model_copy(update=config.overrides)
    executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="eval")
    qdrant = AsyncQdrantClient(url=settings.qdrant_url, timeout=60)
    model = FastEmbedReranker(settings) if "reranker_model" in config.overrides else reranker
    retriever = Retriever(qdrant, settings, embedder, model, executor)
    doc_ids = [doc_id(d.name) for d in DOCS]
    ranked: dict[str, list[str]] = {}
    latencies: list[float] = []
    try:
        await retriever.search(
            user_id=USER_ID, query="warm up", document_ids=doc_ids, mode=config.mode, top_k=1
        )
        for q in queries:
            result = await retriever.search(
                user_id=USER_ID, query=q.text, document_ids=doc_ids, mode=config.mode, top_k=max(KS)
            )
            ranked[q.qid] = [c.hit.chunk_id for c in result.chunks]
            latencies.append(result.timings_ms["total"])
            if result.degraded:
                raise RuntimeError(f"{config.name} {q.qid} degraded: {result.degraded}")
    finally:
        await qdrant.close()
        executor.shutdown(wait=True)
    by_style: dict[str, Any] = {}
    for style in sorted({q.style for q in queries}):
        runs = [(ranked[q.qid], truth[q.qid]) for q in queries if q.style == style]
        by_style[style] = summarize(runs, KS)
    by_style["all"] = summarize([(ranked[q.qid], truth[q.qid]) for q in queries], KS)
    return {
        "metrics": by_style,
        "latency_ms": {
            "median": statistics.median(latencies),
            "p95": pct(latencies, 0.95),
            "max": max(latencies),
        },
        "ranked": ranked,
    }


def snippet(text: str, n: int = 110) -> str:
    one = " ".join(text.split())
    return one if len(one) <= n else one[: n - 1] + "…"


def write_reports(
    results: dict[str, Any],
    queries: list[Query],
    truth: dict[str, set[str]],
    texts: dict[str, str],
    meta: dict[str, Any],
) -> None:
    REPORTS.mkdir(parents=True, exist_ok=True)
    slim = {name: {k: v for k, v in r.items() if k != "ranked"} for name, r in results.items()}
    (REPORTS / "retrieval_eval.json").write_text(
        json.dumps({"meta": meta, "results": slim}, indent=2), encoding="utf-8"
    )
    lines = [
        "# Retrieval evaluation (synthetic corpus, real models)",
        "",
        f"Dataset `{DATASET_VERSION}`: {meta['documents']} documents, {meta['pages']} pages, "
        f"{meta['chunks']} chunks (production Docling pipeline), {meta['questions']} questions in two "
        "styles (natural question; AUTO-DERIVED keyword query = the section heading). Relevant = "
        "chunk containing the answer sentence.",
        "",
        f"Models: dense `{meta['dense_model']}`, sparse `{meta['sparse_model']}`, reranker "
        f"`{meta['reranker']}`. Depth: dense/sparse {meta['dense_limit']} each, RRF k={meta['rrf_k']}, "
        f"final top-{max(KS)}. Hardware: {meta['hardware']}. Date: {meta['date']}.",
        "",
        "HONEST LIMITS: small synthetic corpus; ranks configurations on this data only. "
        "Differences of a few points on 36 questions are within noise (one question = 2.8 points).",
        "",
    ]
    for style in ("natural", "heading_keywords", "all"):
        lines += [
            f"## Query style: {style}",
            "",
            "| Configuration | hit@1 | hit@3 | hit@5 | recall@5 | prec@3 | MRR | NDCG@5 | NDCG@10 | median ms | p95 ms |",
            "|---|---|---|---|---|---|---|---|---|---|---|",
        ]
        for name, r in results.items():
            m = r["metrics"][style]
            lat = r["latency_ms"]
            lines.append(
                f"| {name} | {m['hit@1']:.2f} | {m['hit@3']:.2f} | {m['hit@5']:.2f} | "
                f"{m['recall@5']:.2f} | {m['precision@3']:.2f} | {m['mrr']:.2f} | {m['ndcg@5']:.2f} | "
                f"{m['ndcg@10']:.2f} | {lat['median']:.0f} | {lat['p95']:.0f} |"
            )
        lines.append("")
    (REPORTS / "retrieval_eval.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    # manual-inspection material: queries where the configurations disagree, with the evidence
    def first_rank(name: str, qid: str) -> int | None:
        for position, cid in enumerate(results[name]["ranked"][qid], start=1):
            if cid in truth[qid]:
                return position
        return None

    names = list(results)
    interesting: list[tuple[str, Query]] = []
    for q in queries:
        if q.style != "natural":
            continue
        ranks = {n: first_rank(n, q.qid) for n in names[:4]}
        d, s, h, r = (ranks[n] for n in names[:4])
        if h is None or (h or 99) > 3:
            interesting.append(("hybrid misses top-3", q))
        elif (r or 99) < (h or 99):
            interesting.append(("rerank improved over hybrid", q))
        elif (r or 99) > (h or 99):
            interesting.append(("rerank hurt vs hybrid", q))
        elif (d or 99) != (s or 99):
            interesting.append(("dense and sparse disagree", q))
    out = [
        "# Retrieval inspection (natural-style questions; first relevant rank per configuration)",
        "",
        "Generated by run_eval.py; the written assessment is in docs/ARCHITECTURE_DECISIONS.md (ADR-022).",
        "",
    ]
    for reason, q in interesting[:14]:
        out += [f"## {q.qid} [{reason}]: {q.text}", ""]
        out.append(
            "ranks (dense/sparse/hybrid/hybrid+rerank): "
            + "/".join(str(first_rank(n, q.qid)) for n in names[:4])
        )
        best = next(iter(truth[q.qid]), None)
        if best:
            out.append(f"- RELEVANT: {snippet(texts[best], 220)}")
        for n in (
            "hybrid (RRF k=60)",
            "hybrid + rerank (DEFAULT: bge-reranker-v2-m3 int8, 5 candidates)",
        ):
            top = results[n]["ranked"][q.qid][:3]
            out.append(f"- {n} top-3:")
            for cid in top:
                mark = "✔" if cid in truth[q.qid] else " "
                out.append(f"  - [{mark}] {snippet(texts[cid])}")
        out.append("")
    (REPORTS / "retrieval_inspection.md").write_text("\n".join(out) + "\n", encoding="utf-8")


async def main_async() -> None:
    import platform

    base = get_settings().model_copy(
        update={
            "qdrant_collection_prefix": "eval_",
            "pdf_pipeline": "docling",
            "rerank_timeout_s": 300.0,  # first use downloads the model
            "retrieval_timeout_s": 300.0,
        }
    )
    embedder = FastEmbedEmbedder(base)
    reranker = FastEmbedReranker(base)
    started = time.perf_counter()
    chunks_by_doc = await asyncio.to_thread(index_corpus, base, embedder)
    print(f"indexing took {time.perf_counter() - started:.1f}s", flush=True)

    queries = build_queries()
    truth = {q.qid: relevant_ids(q, chunks_by_doc) for q in queries}
    unscoreable = [q.qid for q in queries if not truth[q.qid]]
    texts = {
        point_id(doc_id(name), 1, c.index): c.text
        for name, chunks in chunks_by_doc.items()
        for c in chunks
    }
    print(
        f"{len(queries)} queries, {len(unscoreable)} without a relevant chunk (excluded)",
        flush=True,
    )

    # Per-configuration cache so a slow cross-encoder is not re-run when only another config is
    # added. Valid only for the same dataset version and models; pass --fresh to ignore it.
    cache = {} if "--fresh" in sys.argv else load_cache(base)
    results: dict[str, Any] = {}
    for config in CONFIGS:
        if config.name in cache:
            results[config.name] = cache[config.name]
            print(f"{config.name}: (cached)", flush=True)
            continue
        results[config.name] = await run_config(
            config, base, embedder, reranker, queries, truth, texts
        )
        save_cache(base, results)
        m = results[config.name]["metrics"]["natural"]
        print(
            f"{config.name}: natural hit@1={m['hit@1']:.2f} hit@3={m['hit@3']:.2f} "
            f"mrr={m['mrr']:.2f} ndcg@5={m['ndcg@5']:.2f}",
            flush=True,
        )
    meta = {
        "dataset_version": DATASET_VERSION,
        "documents": len(DOCS),
        "pages": sum(len(build_pages(d)) for d in DOCS),
        "chunks": sum(len(c) for c in chunks_by_doc.values()),
        "questions": len(_facts()),
        "unscoreable_queries": unscoreable,
        "dense_model": base.embedding_model,
        "sparse_model": base.sparse_model,
        "reranker": base.reranker_model,
        "dense_limit": base.retrieval_dense_limit,
        "rrf_k": base.rrf_k,
        "hardware": f"{platform.processor() or platform.machine()}, {platform.system()} CPU only",
        "date": time.strftime("%Y-%m-%d"),
    }
    write_reports(results, queries, truth, texts, meta)
    client = QdrantClient(url=base.qdrant_url, timeout=30)
    try:
        client.delete_collection(collection_name(base))
    finally:
        client.close()
    print("reports written to evaluation/reports/", flush=True)


if __name__ == "__main__":
    asyncio.run(main_async())
