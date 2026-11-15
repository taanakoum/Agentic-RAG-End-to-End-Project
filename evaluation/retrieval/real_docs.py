"""Retrieval evaluation on REAL public documents (KI-018).

Reproduce (needs the dev Qdrant on 127.0.0.1:6333 and downloads the documents once):

    $env:PYTHONPATH = "backend;."
    uv run --no-sync python evaluation/retrieval/real_docs.py --download      # fetch + verify hashes
    uv run --no-sync python evaluation/retrieval/real_docs.py                  # evaluate + write reports

What it does
  1. `--download` fetches the documents listed in `evaluation/datasets/real_docs_v1.json` to
     `data/real_docs/` (git-ignored: the files are never committed) and verifies their SHA-256.
  2. Parses and chunks them with the PRODUCTION pipeline (sandboxed Docling, NFKC-normalised
     chunks); the chunks are cached in `data/real_docs/chunks_cache.json` (parsing the 80-page
     standard takes minutes) and the cache is tied to the document hashes.
  3. Embeds with the real models, indexes into a throw-away Qdrant collection (prefix `eval_`) as ONE
     user's library, runs dense / BM25 / hybrid / hybrid + rerank through the PRODUCTION Retriever
     and scores them against hand-written questions.

Ground truth: the same rule as `run_eval.py`. Every question carries the answer-bearing sentence,
copied by hand from the document; a chunk is relevant iff its text contains that sentence
(whitespace/hyphen/case-insensitive). A question whose sentence is in no chunk is reported and
excluded (it would mean the chunker split the sentence).

HONEST LIMITS: two documents (95 pages), 28 questions written by the project author after reading
the documents (one question = 3.6 points), one embedding model, one machine. The questions are
natural-language and were written to be answerable from one passage; multi-passage and
unanswerable questions are not part of this retrieval set (the generation evaluation covers
refusals). Wording overlap between a question and its sentence is not controlled, so lexical
retrieval may look better than on questions written without seeing the text. The ground truth is
strict (one sentence = one chunk): when the document states the same rule in two places (the NIST
text repeats the 8-character minimum in sections 5.1.1.1 and 5.1.1.2) a hit on the other place counts
as a miss, so the absolute numbers are pessimistic.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import platform
import statistics
import sys
import time
import urllib.request
import uuid
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from qdrant_client import AsyncQdrantClient, QdrantClient

from app.core.config import REPO_ROOT, Settings, get_settings
from app.ingestion.chunking import Chunk
from app.ingestion.embedding import Embedder, get_embedder
from app.ingestion.indexing import collection_name, ensure_collection, point_id, upsert_chunks
from app.ingestion.parsing import SandboxedPdfParser, parse_bytes
from app.retrieval.models import RetrievalMode
from app.retrieval.rerank import FastEmbedReranker, Reranker
from app.retrieval.retriever import Retriever
from evaluation.retrieval.metrics import summarize

DATASET = Path(__file__).resolve().parents[1] / "datasets" / "real_docs_v1.json"
REPORTS = Path(__file__).resolve().parents[1] / "reports"
DATA_DIR = REPO_ROOT / "data" / "real_docs"
CHUNKS_CACHE = DATA_DIR / "chunks_cache.json"
KS = (1, 3, 5, 10)
USER_ID = uuid.uuid5(uuid.NAMESPACE_URL, "retrieval-real-docs-user")


@dataclass(frozen=True)
class RealDoc:
    name: str
    title: str
    url: str
    sha256: str
    filename: str

    @property
    def path(self) -> Path:
        return DATA_DIR / self.filename


@dataclass(frozen=True)
class RealQuestion:
    qid: str
    doc: str
    question: str
    sentence: str


def load_dataset() -> tuple[str, list[RealDoc], list[RealQuestion]]:
    raw = json.loads(DATASET.read_text(encoding="utf-8"))
    docs = [RealDoc(**d) for d in raw["documents"]]
    questions = [
        RealQuestion(q["id"], q["doc"], q["question"], q["sentence"]) for q in raw["questions"]
    ]
    return str(raw["version"]), docs, questions


def squash(text: str) -> str:
    """Whitespace-, hyphenation- and case-insensitive form used to find the answer sentence."""
    return "".join(ch for ch in text.lower() if ch.isalnum())


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def doc_id(name: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"retrieval-real-docs/{name}")


# ------------------------------------------------------------------ download and verify


def download(docs: Sequence[RealDoc]) -> None:
    """Fetch every document that is missing or has the wrong hash; fail loudly on a mismatch."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    for doc in docs:
        if doc.path.exists() and sha256_of(doc.path) == doc.sha256:
            print(f"{doc.name}: present, hash verified", flush=True)
            continue
        print(f"{doc.name}: downloading {doc.url}", flush=True)
        request = urllib.request.Request(doc.url, headers={"User-Agent": "agentic-rag-eval/1.0"})  # noqa: S310
        with urllib.request.urlopen(request, timeout=120) as response:  # noqa: S310
            data = response.read()
        found = hashlib.sha256(data).hexdigest()
        if found != doc.sha256:
            raise SystemExit(
                f"{doc.name}: SHA-256 mismatch (expected {doc.sha256}, got {found}): the file at "
                "the URL has changed; review it before updating the dataset."
            )
        doc.path.write_bytes(data)
        print(f"{doc.name}: downloaded {len(data)} bytes, hash verified", flush=True)


def require_files(docs: Sequence[RealDoc]) -> None:
    for doc in docs:
        if not doc.path.exists() or sha256_of(doc.path) != doc.sha256:
            raise SystemExit(f"{doc.path} is missing or has a wrong hash: run with --download")


# ------------------------------------------------------------------ parse (cached) and index


def parse_documents(settings: Settings, docs: Sequence[RealDoc]) -> dict[str, list[Chunk]]:
    """Chunks per document from the production pipeline, cached by document hash."""
    cache: dict[str, Any] = {}
    try:
        cache = json.loads(CHUNKS_CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cache = {}
    out: dict[str, list[Chunk]] = {}
    parser: SandboxedPdfParser | None = None
    try:
        for doc in docs:
            entry = cache.get(doc.name)
            if entry and entry.get("sha256") == doc.sha256:
                out[doc.name] = [Chunk(**c) for c in entry["chunks"]]
                print(f"{doc.name}: {len(out[doc.name])} chunks (cached)", flush=True)
                continue
            parser = parser or SandboxedPdfParser(settings)
            started = time.perf_counter()
            inspected = parser.inspect(doc.path)
            parsed = parse_bytes(
                parser,
                doc.path.read_bytes(),
                title=doc.title,
                page_count=inspected.page_count,
                source_format="pdf",
            )
            out[doc.name] = parsed.chunks
            cache[doc.name] = {
                "sha256": doc.sha256,
                "chunks": [dataclasses.asdict(c) for c in parsed.chunks],
            }
            CHUNKS_CACHE.write_text(json.dumps(cache), encoding="utf-8")
            print(
                f"{doc.name}: {inspected.page_count} pages, {len(parsed.chunks)} chunks via "
                f"{parsed.parser} in {time.perf_counter() - started:.0f}s",
                flush=True,
            )
    finally:
        if parser is not None:
            parser.close()
    return out


def index_documents(
    settings: Settings,
    embedder: Embedder,
    docs: Sequence[RealDoc],
    chunks_by_doc: dict[str, list[Chunk]],
    *,
    reset: bool = True,
) -> None:
    client = QdrantClient(url=settings.qdrant_url, timeout=60)
    try:
        if reset and client.collection_exists(collection_name(settings)):
            client.delete_collection(collection_name(settings))
        ensure_collection(client, settings)
        for doc in docs:
            chunks = chunks_by_doc[doc.name]
            texts = [c.embed_text or c.text for c in chunks]
            upsert_chunks(
                client,
                settings,
                user_id=USER_ID,
                document_id=doc_id(doc.name),
                version=1,
                filename=doc.filename,
                chunks=chunks,
                dense=embedder.embed_dense(texts),
                sparse=embedder.embed_sparse(texts),
                embedding_model=embedder.model_name,
            )
            print(f"{doc.name}: indexed {len(chunks)} chunks", flush=True)
    finally:
        client.close()


def drop_collection(settings: Settings) -> None:
    client = QdrantClient(url=settings.qdrant_url, timeout=30)
    try:
        client.delete_collection(collection_name(settings))
    finally:
        client.close()


def relevant_ids(question: RealQuestion, chunks_by_doc: dict[str, list[Chunk]]) -> set[str]:
    key = squash(question.sentence)
    return {
        point_id(doc_id(question.doc), 1, c.index)
        for c in chunks_by_doc[question.doc]
        if key in squash(c.text)
    }


def pct(values: Sequence[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(q * (len(ordered) - 1)))]


# ------------------------------------------------------------------ evaluation


@dataclass(frozen=True)
class Config:
    name: str
    mode: RetrievalMode


CONFIGS = (
    Config("dense", RetrievalMode.DENSE),
    Config("sparse (BM25)", RetrievalMode.SPARSE),
    Config("hybrid (RRF k=60)", RetrievalMode.HYBRID),
    Config("hybrid + rerank (DEFAULT)", RetrievalMode.HYBRID_RERANK),
)


async def run_config(
    config: Config,
    settings: Settings,
    embedder: Embedder,
    reranker: Reranker,
    docs: Sequence[RealDoc],
    questions: Sequence[RealQuestion],
    truth: dict[str, set[str]],
) -> dict[str, Any]:
    executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="eval")
    qdrant = AsyncQdrantClient(url=settings.qdrant_url, timeout=60)
    retriever = Retriever(qdrant, settings, embedder, reranker, executor)
    doc_ids = [doc_id(d.name) for d in docs]
    ranked: dict[str, list[str]] = {}
    latencies: list[float] = []
    try:
        await retriever.search(
            user_id=USER_ID, query="warm up", document_ids=doc_ids, mode=config.mode, top_k=1
        )
        for q in questions:
            result = await retriever.search(
                user_id=USER_ID,
                query=q.question,
                document_ids=doc_ids,
                mode=config.mode,
                top_k=max(KS),
            )
            if result.degraded:
                raise RuntimeError(f"{config.name} {q.qid} degraded: {result.degraded}")
            ranked[q.qid] = [c.hit.chunk_id for c in result.chunks]
            latencies.append(result.timings_ms["total"])
    finally:
        await qdrant.close()
        executor.shutdown(wait=True)
    scoreable = [q for q in questions if truth[q.qid]]
    by_doc = {
        d.name: summarize([(ranked[q.qid], truth[q.qid]) for q in scoreable if q.doc == d.name], KS)
        for d in docs
        if any(q.doc == d.name for q in scoreable)
    }
    return {
        "metrics": summarize([(ranked[q.qid], truth[q.qid]) for q in scoreable], KS),
        "by_document": by_doc,
        "latency_ms": {
            "median": statistics.median(latencies),
            "p95": pct(latencies, 0.95),
            "max": max(latencies),
        },
        "ranked": ranked,
    }


def first_rank(ranked: Sequence[str], relevant: set[str]) -> int | None:
    for position, cid in enumerate(ranked, start=1):
        if cid in relevant:
            return position
    return None


def write_reports(
    results: dict[str, Any],
    questions: Sequence[RealQuestion],
    truth: dict[str, set[str]],
    meta: dict[str, Any],
) -> None:
    REPORTS.mkdir(parents=True, exist_ok=True)
    slim = {name: {k: v for k, v in r.items() if k != "ranked"} for name, r in results.items()}
    (REPORTS / "retrieval_real_docs.json").write_text(
        json.dumps({"meta": meta, "results": slim}, indent=2), encoding="utf-8"
    )
    lines = [
        "# Retrieval evaluation on real public documents",
        "",
        f"Dataset `{meta['dataset_version']}`: {meta['documents']}. {meta['chunks']} chunks from the "
        f"production Docling pipeline, {meta['scoreable']} scoreable questions "
        f"({meta['questions']} written). Relevant = a chunk containing the answer sentence copied "
        "from the document. The documents themselves are not in the repository "
        "(`python evaluation/retrieval/real_docs.py --download` fetches and hash-checks them).",
        "",
        f"Models: dense `{meta['dense_model']}`, sparse `{meta['sparse_model']}`, reranker "
        f"`{meta['reranker']}` ({meta['rerank_candidates']} candidates). Depth: dense/sparse "
        f"{meta['dense_limit']} each, RRF k={meta['rrf_k']}, top-{max(KS)}. Hardware: "
        f"{meta['hardware']}. Date: {meta['date']}.",
        "",
        "HONEST LIMITS: two documents, one author of questions, one embedding model, one machine. "
        f"One question = {100 / meta['scoreable']:.1f} points, so differences of one or two "
        "questions are noise. Questions were written after reading the text and are not controlled "
        "for word overlap with the answer sentence. The ground truth is strict (one sentence = one chunk): "
        "when the document states the same rule in two places, a hit on the other place counts as a "
        "miss, so absolute numbers are pessimistic.",
        "",
        "| Configuration | hit@1 | hit@3 | hit@5 | hit@10 | recall@5 | MRR | NDCG@5 | median ms | p95 ms |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for name, r in results.items():
        m, lat = r["metrics"], r["latency_ms"]
        lines.append(
            f"| {name} | {m['hit@1']:.2f} | {m['hit@3']:.2f} | {m['hit@5']:.2f} | {m['hit@10']:.2f} "
            f"| {m['recall@5']:.2f} | {m['mrr']:.2f} | {m['ndcg@5']:.2f} | {lat['median']:.0f} | "
            f"{lat['p95']:.0f} |"
        )
    lines += ["", "## Per document (hit@1 / hit@3 / MRR)", ""]
    docs = sorted({d for r in results.values() for d in r["by_document"]})
    lines += ["| Configuration | " + " | ".join(docs) + " |", "|---|" + "---|" * len(docs)]
    for name, r in results.items():
        cells: list[str] = []
        for d in docs:
            m = r["by_document"][d]
            cells.append(
                f"{m['hit@1']:.2f} / {m['hit@3']:.2f} / {m['mrr']:.2f} (n={int(m['queries'])})"
            )
        lines.append(f"| {name} | " + " | ".join(cells) + " |")
    lines += ["", "## Questions where the configurations disagree (first relevant rank)", ""]
    names = list(results)
    lines += [
        "| id | question | " + " | ".join(names) + " |",
        "|---|---|" + "---|" * len(names),
    ]
    for q in questions:
        if not truth[q.qid]:
            continue
        ranks = [first_rank(results[n]["ranked"][q.qid], truth[q.qid]) for n in names]
        if len(set(ranks)) > 1 or (ranks[-1] or 99) > 1:
            lines.append(
                f"| {q.qid} | {q.question} | "
                + " | ".join(str(r) if r else "-" for r in ranks)
                + " |"
            )
    if meta["unscoreable"]:
        lines += ["", f"Excluded (answer sentence not found in one chunk): {meta['unscoreable']}"]
    (REPORTS / "retrieval_real_docs.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


async def main_async() -> None:
    version, docs, questions = load_dataset()
    require_files(docs)
    base = get_settings().model_copy(
        update={
            "qdrant_collection_prefix": "eval_",
            "pdf_pipeline": "docling",
            "rerank_timeout_s": 300.0,
            "retrieval_timeout_s": 300.0,
            "retrieval_cache_enabled": False,
        }
    )
    embedder = get_embedder(base)  # local ONNX, or hosted when EMBEDDING_BACKEND=hosted
    reranker = FastEmbedReranker(base)
    chunks_by_doc = await asyncio.to_thread(parse_documents, base, docs)
    await asyncio.to_thread(index_documents, base, embedder, docs, chunks_by_doc)
    try:
        truth = {q.qid: relevant_ids(q, chunks_by_doc) for q in questions}
        unscoreable = [q.qid for q in questions if not truth[q.qid]]
        print(
            f"{len(questions)} questions, {len(unscoreable)} without a relevant chunk", flush=True
        )
        results: dict[str, Any] = {}
        for config in CONFIGS:
            results[config.name] = await run_config(
                config, base, embedder, reranker, docs, questions, truth
            )
            m = results[config.name]["metrics"]
            print(
                f"{config.name}: hit@1={m['hit@1']:.2f} hit@3={m['hit@3']:.2f} mrr={m['mrr']:.2f}",
                flush=True,
            )
    finally:
        await asyncio.to_thread(drop_collection, base)
    meta = {
        "dataset_version": version,
        "documents": ", ".join(d.title for d in docs),
        "chunks": sum(len(c) for c in chunks_by_doc.values()),
        "questions": len(questions),
        "scoreable": len(questions) - len(unscoreable),
        "unscoreable": unscoreable,
        "dense_model": base.embedding_model,
        "sparse_model": base.sparse_model,
        "reranker": base.reranker_model,
        "rerank_candidates": base.rerank_candidates,
        "dense_limit": base.retrieval_dense_limit,
        "rrf_k": base.rrf_k,
        "hardware": f"{platform.processor() or platform.machine()}, {platform.system()} CPU only",
        "date": time.strftime("%Y-%m-%d"),
    }
    write_reports(results, questions, truth, meta)
    print("reports written to evaluation/reports/retrieval_real_docs.*", flush=True)


def main() -> None:
    if "--download" in sys.argv:
        _, docs, _ = load_dataset()
        download(docs)
        return
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
