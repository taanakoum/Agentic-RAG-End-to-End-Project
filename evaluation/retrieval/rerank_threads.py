"""Interleaved latency check of ONNX thread settings for the default cross-encoder (KI-023).

    $env:PYTHONPATH = "backend;."
    uv run --no-sync python evaluation/retrieval/rerank_threads.py

`rerank_bench.py` measures one configuration after the other; on a laptop that drifts (thermal
throttling, background work), which made its first latency table contradictory. This script loads
one model per thread setting and calls them in ROUND-ROBIN order on the same passages, so drift
hits every setting equally. Passages: the real-document chunks the benchmark cached (the length of
real chunks matters for cross-encoder cost). Results: evaluation/reports/rerank_threads.md/json.
"""

from __future__ import annotations

import json
import os
import platform
import statistics
import time
from pathlib import Path

from app.core.config import get_settings
from app.retrieval.rerank import FastEmbedReranker

ROOT = Path(__file__).resolve().parents[2]
REPORTS = ROOT / "evaluation" / "reports"
SETTINGS = (0, 2, 4, 8)  # 0 = ONNX Runtime default
ROUNDS = 6
CANDIDATES = 5


def passages() -> list[list[str]]:
    """Five 2000-character candidate passages per question, built from the real-document chunks."""
    cache = ROOT / "evaluation" / ".cache" / "real_docs_chunks.json"
    texts: list[str] = []
    if cache.exists():
        data = json.loads(cache.read_text(encoding="utf-8"))
        for chunks in data.values():
            texts.extend(str(c.get("text", "")) for c in chunks if c.get("text"))
    if len(texts) < CANDIDATES * 8:
        base = "The verifier SHALL limit consecutive failed authentication attempts. " * 40
        texts = [base[i : i + 2000] for i in range(0, 50 * 37, 37)]
    texts = [t[:2000] for t in texts]
    return [texts[i : i + CANDIDATES] for i in range(0, CANDIDATES * 8, CANDIDATES)]


def main() -> None:
    settings = get_settings()
    rerankers = {
        n: FastEmbedReranker(settings.model_copy(update={"rerank_threads": n})) for n in SETTINGS
    }
    groups = passages()
    query = "How many failed authentication attempts may a verifier allow?"
    for reranker in rerankers.values():  # load and warm every model first
        reranker.score(query, groups[0])
    timings: dict[int, list[float]] = {n: [] for n in SETTINGS}
    for _ in range(ROUNDS):
        for group in groups:
            for n, reranker in rerankers.items():  # round-robin: drift affects all settings alike
                start = time.perf_counter()
                reranker.score(query, group)
                timings[n].append((time.perf_counter() - start) * 1000)
    rows: list[dict[str, object]] = []
    for n in SETTINGS:
        values = sorted(timings[n])
        rows.append(
            {
                "threads": n or "default",
                "calls": len(values),
                "p50_ms": round(statistics.median(values)),
                "p95_ms": round(values[int(0.95 * (len(values) - 1))]),
            }
        )
    meta = {
        "model": settings.reranker_model,
        "candidates": CANDIDATES,
        "max_chars": 2000,
        "cpu": platform.processor(),
        "logical_cores": os.cpu_count(),
    }
    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / "rerank_threads.json").write_text(
        json.dumps({"meta": meta, "rows": rows}, indent=2), encoding="utf-8"
    )
    lines = [
        "# Cross-encoder ONNX threads (interleaved measurement)",
        "",
        f"Model `{meta['model']}`, {CANDIDATES} candidates of up to 2000 characters, "
        f"{meta['logical_cores']} logical cores ({meta['cpu']}); every setting was called in "
        f"round-robin order, {ROUNDS} rounds x 8 questions, after a warm-up call.",
        "",
        "| ONNX threads | calls | p50 ms | p95 ms |",
        "|---|---|---|---|",
        *[f"| {r['threads']} | {r['calls']} | {r['p50_ms']} | {r['p95_ms']} |" for r in rows],
        "",
        "One laptop, one run; the absolute numbers depend on load and power settings.",
        "",
    ]
    (REPORTS / "rerank_threads.md").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
