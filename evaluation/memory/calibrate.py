"""Calibrate the memory relevance threshold (MEMORY_MIN_SCORE) with the REAL embedding model.

    $env:PYTHONPATH = "backend;."; uv run --no-sync python evaluation/memory/calibrate.py

A small, hand-written set: memories a user might keep, queries that SHOULD recall one of them
(paraphrased, not copied) and queries that should recall NOTHING (other topics, generic chat).
For each threshold it reports recall of the relevant queries and the share of irrelevant queries
that wrongly retrieve something. HONEST LIMITS: ~12 memories and 40 queries, English only,
written by the project author; it picks a sensible default, it is not a benchmark.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

from app.core.config import get_settings
from app.ingestion.embedding import FastEmbedEmbedder

REPORTS = Path(__file__).resolve().parents[1] / "reports"

MEMORIES = [
    "The user prefers dark mode in every editor and terminal.",
    "The user's name is Dana.",
    "The user works at Acme Corp as a data engineer.",
    "The user prefers concise answers without long introductions.",
    "Deploys happen on Fridays at 16:00.",
    "The user is vegetarian and avoids restaurants that only serve meat.",
    "The user usually writes code in Python and uses pytest.",
    "The user dislikes long meetings and prefers asynchronous updates.",
    "The user's team uses PostgreSQL and Qdrant for storage.",
    "The user prefers metric units and 24-hour time.",
    "The user is allergic to peanuts.",
    "Quarterly reports are due on the 15th of the month after quarter end.",
]

# (query, index of the memory it should recall)
RELEVANT = [
    ("what colour theme does the user like for their editor", 0),
    ("should the UI default to a dark theme", 0),
    ("what should I call the user", 1),
    ("where does the user work", 2),
    ("what is the user's job title", 2),
    ("how long should my replies be", 3),
    ("does the user like detailed explanations", 3),
    ("when do releases go out", 4),
    ("which day is deployment day", 4),
    ("suggest a restaurant for the user", 5),
    ("what are the user's dietary restrictions", 5),
    ("which programming language and test framework does the user use", 6),
    ("how does the user feel about meetings", 7),
    ("which databases does the team run", 8),
    ("what measurement system should I use", 9),
    ("does the user have any food allergies", 10),
    ("when is the quarterly report deadline", 11),
]
IRRELEVANT = [
    "what is the capital of France",
    "explain how a transformer neural network works",
    "write a haiku about autumn",
    "how do I reverse a linked list",
    "what is the weather like in Tokyo",
    "summarise the plot of Hamlet",
    "convert 100 dollars to euros",
    "who won the football world cup in 2014",
    "how many calories are in an avocado",
    "recommend a good book about history",
    "what does the HTTP 404 status mean",
    "tell me a joke",
    "how far is the moon from the earth",
    "translate good morning into Spanish",
    "what is photosynthesis",
    "list the planets of the solar system",
]
THRESHOLDS = [0.3, 0.35, 0.4, 0.45, 0.5, 0.55, 0.6, 0.65, 0.7]


def main() -> None:
    embedder = FastEmbedEmbedder(get_settings())
    docs = np.array(embedder.embed_dense(MEMORIES))
    rel_scores: list[float] = []
    rel_top1: list[bool] = []
    for query, target in RELEVANT:
        scores = docs @ np.array(embedder.embed_query_dense(query))
        rel_scores.append(float(scores[target]))
        rel_top1.append(int(np.argmax(scores)) == target)
    irr_best: list[float] = []
    for query in IRRELEVANT:
        scores = docs @ np.array(embedder.embed_query_dense(query))
        irr_best.append(float(scores.max()))
    rows: list[dict[str, float]] = []
    for t in THRESHOLDS:
        recall = sum(1 for s in rel_scores if s >= t) / len(rel_scores)
        false_hits = sum(1 for s in irr_best if s >= t) / len(irr_best)
        rows.append(
            {"threshold": t, "recall_relevant": recall, "false_retrieval_irrelevant": false_hits}
        )
    result: dict[str, Any] = {
        "memories": len(MEMORIES),
        "relevant_queries": len(RELEVANT),
        "irrelevant_queries": len(IRRELEVANT),
        "top1_accuracy_relevant": sum(rel_top1) / len(rel_top1),
        "relevant_score_min_median_max": [
            min(rel_scores),
            float(np.median(rel_scores)),
            max(rel_scores),
        ],
        "irrelevant_best_score_min_median_max": [
            min(irr_best),
            float(np.median(irr_best)),
            max(irr_best),
        ],
        "thresholds": rows,
        "model": get_settings().embedding_model,
    }
    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / "memory_threshold.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    lines = [
        "# Memory relevance threshold (real model, hand-written set)",
        "",
        f"Model `{result['model']}`; {result['memories']} memories, {result['relevant_queries']} "
        f"relevant and {result['irrelevant_queries']} irrelevant queries. Top-1 accuracy on the relevant "
        f"queries: {result['top1_accuracy_relevant']:.2f}.",
        "",
        "Cosine score of the target memory (relevant queries): "
        f"min {min(rel_scores):.2f}, median {np.median(rel_scores):.2f}, max {max(rel_scores):.2f}. "
        "Best score any memory gets for an irrelevant query: "
        f"min {min(irr_best):.2f}, median {np.median(irr_best):.2f}, max {max(irr_best):.2f}.",
        "",
        "| threshold | relevant queries recalled | irrelevant queries that retrieve something |",
        "|---|---|---|",
    ]
    for r in rows:
        lines.append(
            f"| {r['threshold']:.2f} | {r['recall_relevant']:.2f} | {r['false_retrieval_irrelevant']:.2f} |"
        )
    lines += [
        "",
        "HONEST LIMITS: tiny hand-written English set; use it to choose a default, not as a benchmark.",
    ]
    (REPORTS / "memory_threshold.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
