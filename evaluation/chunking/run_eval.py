"""Chunking strategy evaluation. Reproduce with:

    $env:PYTHONPATH = "backend;."; uv run --no-sync python evaluation/chunking/run_eval.py

Retrieval uses the REAL embedding models (BAAI/bge-small-en-v1.5 dense + Qdrant/bm25 sparse) over
the whole corpus at once (all documents together, like a user's library). A chunk is relevant to a
question iff its text contains the answer-bearing sentence (whitespace/hyphen-insensitive), so a
strategy that cuts the sentence apart (page break, mid-sentence split) loses the fact entirely.
"""

from __future__ import annotations

import io
import json
import re
import statistics
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from app.core.config import get_settings
from app.ingestion.chunking import chunk_pages, contextual_text, estimate_tokens
from app.ingestion.embedding import FastEmbedEmbedder
from app.ingestion.textclean import clean_pages
from app.ingestion.textnorm import PageText, normalize_text
from evaluation.chunking.corpus import (
    DOCS,
    Fact,
    all_facts,
    build_pages,
    render_pdf,
    straddling_facts,
)

REPORTS = Path(__file__).resolve().parents[1] / "reports"


@dataclass
class EvalChunk:
    doc: str
    raw: str  # chunk text as stored (what a citation would show)
    embedded: str  # text that is embedded / indexed


def squash(text: str) -> str:
    return re.sub(r"[\s\-­]+", "", text.lower())


# ------------------------------------------------------------------ strategies


def _pages(pdf: bytes) -> list[PageText]:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(pdf))
    return [
        PageText(i + 1, normalize_text(page.extract_text() or ""))
        for i, page in enumerate(reader.pages)
    ]


def strat_baseline(doc: str, title: str, pdf: bytes) -> list[EvalChunk]:
    """Old behaviour: raw page text, fixed 1000-char windows with 150 overlap, per page."""
    from langchain_text_splitters import RecursiveCharacterTextSplitter

    splitter = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=150)
    out: list[EvalChunk] = []
    for page in _pages(pdf):
        out += [EvalChunk(doc, t, t) for t in splitter.split_text(page.text) if t.strip()]
    return out


def strat_langchain_clean(doc: str, title: str, pdf: bytes) -> list[EvalChunk]:
    """Cleaned whole-document text, LangChain recursive splitter sized in (estimated) tokens."""
    from langchain_text_splitters import RecursiveCharacterTextSplitter

    text = "\n\n".join(p.text for p in clean_pages(_pages(pdf)))
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=256,
        chunk_overlap=40,
        length_function=estimate_tokens,
        separators=["\n\n", "\n", ". ", " ", ""],
    )
    return [EvalChunk(doc, t, t) for t in splitter.split_text(text) if t.strip()]


def strat_custom(doc: str, title: str, pdf: bytes, *, context: bool = True) -> list[EvalChunk]:
    chunks = chunk_pages(clean_pages(_pages(pdf)))
    return [
        EvalChunk(doc, c.text, contextual_text(c, title=title) if context else c.text)
        for c in chunks
    ]


def strat_custom_no_context(doc: str, title: str, pdf: bytes) -> list[EvalChunk]:
    return strat_custom(doc, title, pdf, context=False)


_converter: Any = None
_docling_cache: dict[str, Any] = {}
docling_convert_seconds = 0.0


def _docling_doc(pdf: bytes, name: str) -> Any:
    global _converter, docling_convert_seconds
    if name in _docling_cache:
        return _docling_cache[name]
    from docling.datamodel.base_models import DocumentStream, InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption

    if _converter is None:
        opts = PdfPipelineOptions(do_ocr=False, do_table_structure=False)
        _converter = DocumentConverter(
            format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)}
        )
    started = time.perf_counter()
    document = _converter.convert(
        DocumentStream(name=f"{name}.pdf", stream=io.BytesIO(pdf))
    ).document
    docling_convert_seconds += time.perf_counter() - started
    _docling_cache[name] = document
    return document


def strat_docling_hybrid(doc: str, title: str, pdf: bytes) -> list[EvalChunk]:
    from docling.chunking import HybridChunker
    from docling_core.transforms.chunker.tokenizer.huggingface import HuggingFaceTokenizer
    from transformers import AutoTokenizer

    tok = HuggingFaceTokenizer(
        tokenizer=AutoTokenizer.from_pretrained("BAAI/bge-small-en-v1.5"), max_tokens=320
    )
    chunker = HybridChunker(tokenizer=tok)
    document = _docling_doc(pdf, doc)
    return [EvalChunk(doc, c.text, chunker.contextualize(c)) for c in chunker.chunk(document)]


def strat_docling_md_langchain(doc: str, title: str, pdf: bytes) -> list[EvalChunk]:
    from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter

    markdown = _docling_doc(pdf, doc).export_to_markdown()
    sections = MarkdownHeaderTextSplitter(
        headers_to_split_on=[("#", "h1"), ("##", "h2"), ("###", "h3")], strip_headers=False
    ).split_text(markdown)
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=256, chunk_overlap=40, length_function=estimate_tokens
    )
    out: list[EvalChunk] = []
    for section in sections:
        path = " > ".join(str(v) for k, v in section.metadata.items() if k.startswith("h"))
        for piece in splitter.split_text(section.page_content):
            if piece.strip():
                out.append(EvalChunk(doc, piece, f"{title} > {path}\n{piece}" if path else piece))
    return out


STRATEGIES = {
    "A baseline (fixed 1000 chars, per page, raw)": strat_baseline,
    "B langchain recursive (clean text, 256 tok)": strat_langchain_clean,
    "C custom, no heading context": strat_custom_no_context,
    "D custom + heading/title context": strat_custom,
    "E docling HybridChunker (320 tok)": strat_docling_hybrid,
    "F docling markdown + langchain headers": strat_docling_md_langchain,
}

# ------------------------------------------------------------------ retrieval + metrics


def _sparse_dot(a: Any, b: Any) -> float:
    lookup = dict(zip(b.indices, b.values, strict=True))
    return sum(v * lookup.get(i, 0.0) for i, v in zip(a.indices, a.values, strict=True))


def evaluate(
    chunks: list[EvalChunk], facts: list[Fact], embedder: FastEmbedEmbedder
) -> dict[str, Any]:
    texts = [c.embedded for c in chunks]
    dense = np.array(embedder.embed_dense(texts))
    sparse = embedder.embed_sparse(texts)
    sparse_model = embedder._sparse_model()
    dense_model = embedder._dense_model()
    squashed = [squash(c.raw) for c in chunks]

    preserved = 0
    ranks_dense: list[int | None] = []
    ranks_hybrid: list[int | None] = []
    for fact in facts:
        key = squash(fact.sentence)
        relevant = {i for i, c in enumerate(chunks) if c.doc == fact.doc and key in squashed[i]}
        if relevant:
            preserved += 1
        q_dense = np.array(next(iter(dense_model.query_embed(fact.question))))
        d_scores = dense @ q_dense
        q_sparse = next(iter(sparse_model.query_embed(fact.question)))
        s_scores = np.array([_sparse_dot(q_sparse, s) for s in sparse])
        d_rank = {int(i): r for r, i in enumerate(np.argsort(-d_scores), start=1)}
        s_rank = {int(i): r for r, i in enumerate(np.argsort(-s_scores), start=1)}
        rrf = {i: 1 / (60 + d_rank[i]) + 1 / (60 + s_rank[i]) for i in d_rank}
        h_order = sorted(rrf, key=lambda i: -rrf[i])
        d_order = sorted(d_rank, key=lambda i: d_rank[i])

        def first_rank(order: list[int], relevant: set[int] = relevant) -> int | None:
            for position, idx in enumerate(order, start=1):
                if idx in relevant:
                    return position
            return None

        ranks_dense.append(first_rank(d_order))
        ranks_hybrid.append(first_rank(h_order))

    def metrics(ranks: list[int | None]) -> dict[str, float]:
        n = len(ranks)
        hit = lambda k: sum(1 for r in ranks if r is not None and r <= k) / n  # noqa: E731
        mrr = sum(1 / r for r in ranks if r) / n
        return {"hit@1": hit(1), "hit@3": hit(3), "hit@5": hit(5), "mrr": mrr}

    tokens = [estimate_tokens(c.raw) for c in chunks]
    return {
        "chunks": len(chunks),
        "mean_tokens": statistics.mean(tokens),
        "max_tokens": max(tokens),
        "fact_preserved": preserved / len(facts),
        "dense": metrics(ranks_dense),
        "hybrid": metrics(ranks_hybrid),
    }


def main() -> None:
    settings = get_settings()
    embedder = FastEmbedEmbedder(settings)
    facts = all_facts()
    pdfs = {d.name: (d.title, render_pdf(build_pages(d))) for d in DOCS}
    page_total = sum(len(build_pages(d)) for d in DOCS)
    straddle = sum(straddling_facts(d, build_pages(d)) for d in DOCS)
    results: dict[str, Any] = {
        "corpus": {
            "documents": len(DOCS),
            "pages": page_total,
            "questions": len(facts),
            "facts_continuing_across_a_page_break": straddle,
        }
    }
    for name, strategy in STRATEGIES.items():
        started = time.perf_counter()
        chunks: list[EvalChunk] = []
        for doc_name, (title, pdf) in pdfs.items():
            chunks += strategy(doc_name, title, pdf)
        elapsed = time.perf_counter() - started
        if name.startswith(("E ", "F ")):
            elapsed = elapsed - 0.0  # conversion time is counted once, reported below
        result = evaluate(chunks, facts, embedder)
        result["chunking_seconds"] = elapsed
        result["seconds_per_page"] = elapsed / page_total
        results[name] = result
        print(
            f"{name}: preserved={result['fact_preserved']:.2f} "
            f"hybrid hit@1={result['hybrid']['hit@1']:.2f} mrr={result['hybrid']['mrr']:.2f} "
            f"chunks={result['chunks']} {elapsed:.1f}s",
            flush=True,
        )
    results["docling_convert_seconds_total"] = docling_convert_seconds
    results["docling_convert_seconds_per_page"] = docling_convert_seconds / page_total
    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / "chunking_eval.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    lines = [
        "# Chunking evaluation (synthetic corpus, real embedding models)",
        "",
        f"Corpus: {len(DOCS)} documents, {page_total} pages, {len(facts)} questions, {straddle} of them whose answer sentence continues across a page break (synthetic, authored for this project; see corpus.py).",
        f"Docling PDF conversion (layout analysis, CPU) took {docling_convert_seconds:.1f}s in total = {docling_convert_seconds / page_total:.2f} s/page; it is included in the s/page of E and F only through this line (their column shows chunking after conversion).",
        "",
        "| Strategy | chunks | mean tok | fact kept | dense hit@1 | dense MRR | hybrid hit@1 | hybrid hit@3 | hybrid hit@5 | hybrid MRR | s/page |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for name in STRATEGIES:
        r = results[name]
        lines.append(
            f"| {name} | {r['chunks']} | {r['mean_tokens']:.0f} | {r['fact_preserved']:.2f} | "
            f"{r['dense']['hit@1']:.2f} | {r['dense']['mrr']:.2f} | {r['hybrid']['hit@1']:.2f} | "
            f"{r['hybrid']['hit@3']:.2f} | {r['hybrid']['hit@5']:.2f} | {r['hybrid']['mrr']:.2f} | "
            f"{r['seconds_per_page']:.2f} |"
        )
    (REPORTS / "chunking_eval.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
