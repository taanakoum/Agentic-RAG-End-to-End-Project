"""Calibrate the memory relevance threshold (MEMORY_MIN_SCORE) with the REAL embedding model.

    $env:PYTHONPATH = "backend;."; uv run --no-sync python evaluation/memory/calibrate.py

A hand-written set (v2): 30 memories a user might keep; queries that SHOULD recall one of them and
queries that should recall NOTHING. Relevant queries come in four groups (short, paraphrased,
long with extra context, and French/German/Spanish); irrelevant ones in three (unrelated, NEAR-MISS
= they share words or a topic with a memory but need nothing from it, and foreign-language
unrelated). For each threshold it reports the recall of the relevant queries and the share of
irrelevant queries that wrongly retrieve something, overall and per group.

HONEST LIMITS: written by the project author in one sitting (labels follow one person's idea of
"relevant"; near-miss labels are debatable by nature), 50 relevant and 31 irrelevant queries, one
embedding model (`BAAI/bge-small-en-v1.5`, an English model: the non-English results are expected to
be weak and are reported to show that, not to tune for them). It picks a sensible default; it is
not a benchmark.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

from app.core.config import get_settings
from app.ingestion.embedding import FastEmbedEmbedder

REPORTS = Path(__file__).resolve().parents[1] / "reports"
DATASET_VERSION = "memory-threshold-v2"

MEMORIES = [
    "The user prefers dark mode in every editor and terminal.",  # 0
    "The user's name is Dana.",  # 1
    "The user works at Acme Corp as a data engineer.",  # 2
    "The user prefers concise answers without long introductions.",  # 3
    "Deploys happen on Fridays at 16:00.",  # 4
    "The user is vegetarian and avoids restaurants that only serve meat.",  # 5
    "The user usually writes code in Python and uses pytest.",  # 6
    "The user dislikes long meetings and prefers asynchronous updates.",  # 7
    "The user's team uses PostgreSQL and Qdrant for storage.",  # 8
    "The user prefers metric units and 24-hour time.",  # 9
    "The user is allergic to peanuts.",  # 10
    "Quarterly reports are due on the 15th of the month after quarter end.",  # 11
    "The user's manager is called Priya and they meet every Tuesday.",  # 12
    "The user is learning Spanish and practises for 20 minutes every morning.",  # 13
    "The user commutes by bicycle and avoids routes with heavy traffic.",  # 14
    "The project codename is Falcon and it launches in March.",  # 15
    "The user wants answers as bullet lists, with code samples in Python.",  # 16
    "The user's cat is called Miso.",  # 17
    "The user works from home on Mondays and Wednesdays at a standing desk.",  # 18
    "The user's laptop runs Linux and their phone runs iOS.",  # 19
    "The user's cloud spending budget is 2,000 euros per month.",  # 20
    "The user speaks German and English at work.",  # 21
    "The user is training for a half marathon in October.",  # 22
    "The user dislikes emoji in technical documents.",  # 23
    "The team stand-up is at 09:30 Central European Time.",  # 24
    "The user prefers PDF exports over Word documents for sharing.",  # 25
    "The user reads research papers about retrieval and ranking in the evenings.",  # 26
    "The company's fiscal year starts in July.",  # 27
    "The user takes the train to the Munich office once a week.",  # 28
    "The user wants code reviews to focus on security and test coverage first.",  # 29
]

# (group, query, index of the memory it should recall)
RELEVANT: list[tuple[str, str, int]] = [
    # --- short (the v1 set, plus short questions about the newer memories)
    ("short", "what colour theme does the user like for their editor", 0),
    ("short", "should the UI default to a dark theme", 0),
    ("short", "what should I call the user", 1),
    ("short", "where does the user work", 2),
    ("short", "what is the user's job title", 2),
    ("short", "how long should my replies be", 3),
    ("short", "does the user like detailed explanations", 3),
    ("short", "when do releases go out", 4),
    ("short", "which day is deployment day", 4),
    ("short", "suggest a restaurant for the user", 5),
    ("short", "what are the user's dietary restrictions", 5),
    ("short", "which programming language and test framework does the user use", 6),
    ("short", "how does the user feel about meetings", 7),
    ("short", "which databases does the team run", 8),
    ("short", "what measurement system should I use", 9),
    ("short", "does the user have any food allergies", 10),
    ("short", "when is the quarterly report deadline", 11),
    ("short", "who is the user's manager", 12),
    ("short", "which language is the user studying", 13),
    ("short", "how does the user get to work", 14),
    ("short", "when does the project launch", 15),
    ("short", "what format should answers be in", 16),
    ("short", "what is the user's pet called", 17),
    ("short", "which days does the user work from home", 18),
    ("short", "which operating system does the user's laptop run", 19),
    ("short", "which languages does the user speak", 21),
    ("short", "when is the daily stand-up", 24),
    # --- paraphrased: other words for the same need
    ("paraphrase", "is the user comfortable with a bright light theme in the IDE", 0),
    ("paraphrase", "which company employs the user and in what role", 2),
    ("paraphrase", "skip the preamble and get straight to the point", 3),
    ("paraphrase", "can the user sit through a long weekly all-hands meeting", 7),
    ("paraphrase", "is the user able to enjoy a steak dinner", 5),
    ("paraphrase", "is it safe to recommend a satay sauce made with groundnuts", 10),
    ("paraphrase", "at what hour is the production release window", 4),
    ("paraphrase", "which tools does the data team rely on to keep embeddings and records", 8),
    # --- long: a task description with the need buried in it
    (
        "long",
        "I'm drafting a plan for the next sprint and I need to know when releases usually go out "
        "so that I can avoid scheduling risky changes at the wrong time",
        4,
    ),
    (
        "long",
        "We are choosing a place for the team dinner next week and I'd like to make sure everyone, "
        "including the person asking you, can actually eat something on the menu",
        5,
    ),
    (
        "long",
        "Before I write the unit tests for this module, remind me which programming language and "
        "which test runner this person normally prefers to work with",
        6,
    ),
    (
        "long",
        "I am about to send the weekly project status email and I want to format the numbers the "
        "way the user normally likes, including how the temperature and the time of day are written",
        9,
    ),
    (
        "long",
        "Could you review this very long document and give me your answer in the style that this "
        "user generally finds the most useful and least tiring to read",
        3,
    ),
    (
        "long",
        "For the budget forecast I have to know how much money can be spent each month on cloud "
        "infrastructure so please tell me what limit applies",
        20,
    ),
    (
        "long",
        "Plan a training schedule that fits around the upcoming race this person has signed up "
        "for in the autumn",
        22,
    ),
    # --- French / German / Spanish (an English-only embedding model: expected to be weak)
    ("multilingual", "quel thème de couleur l'utilisateur préfère-t-il pour son éditeur", 0),
    ("multilingual", "où travaille l'utilisateur", 2),
    ("multilingual", "quel jour fait-on les déploiements en production", 4),
    ("multilingual", "wie soll ich den Nutzer nennen", 1),
    ("multilingual", "welche Programmiersprache benutzt der Nutzer", 6),
    ("multilingual", "hat der Nutzer eine Lebensmittelallergie", 10),
    ("multilingual", "¿en qué día se hacen los despliegues?", 4),
    ("multilingual", "¿qué sistema de unidades prefiere el usuario?", 9),
]

# (group, query): nothing in the memories is needed to answer these
IRRELEVANT: list[tuple[str, str]] = [
    ("unrelated", "what is the capital of France"),
    ("unrelated", "explain how a transformer neural network works"),
    ("unrelated", "write a haiku about autumn"),
    ("unrelated", "how do I reverse a linked list"),
    ("unrelated", "what is the weather like in Tokyo"),
    ("unrelated", "summarise the plot of Hamlet"),
    ("unrelated", "convert 100 dollars to euros"),
    ("unrelated", "who won the football world cup in 2014"),
    ("unrelated", "how many calories are in an avocado"),
    ("unrelated", "recommend a good book about history"),
    ("unrelated", "what does the HTTP 404 status mean"),
    ("unrelated", "tell me a joke"),
    ("unrelated", "how far is the moon from the earth"),
    ("unrelated", "translate good morning into Spanish"),
    ("unrelated", "what is photosynthesis"),
    ("unrelated", "list the planets of the solar system"),
    # near-miss: shares a word or a topic with a memory, but needs nothing from it
    ("near_miss", "what is the history of dark matter in cosmology"),
    ("near_miss", "explain the PostgreSQL write-ahead log"),
    ("near_miss", "describe the causes of the Spanish Civil War"),
    ("near_miss", "who is the current manager of Manchester United"),
    ("near_miss", "how fast can a falcon dive"),
    ("near_miss", "how long do cats usually live"),
    ("near_miss", "what does the German word Schadenfreude mean"),
    ("near_miss", "how many emoji are defined in Unicode"),
    ("near_miss", "how do train brakes work"),
    ("near_miss", "what is the fiscal policy of the European Central Bank"),
    ("near_miss", "what is the world record for the marathon"),
    ("near_miss", "who invented the standing ovation tradition"),
    # unrelated, in other languages
    ("foreign_unrelated", "quelle est la capitale de l'Allemagne"),
    ("foreign_unrelated", "¿cuál es la capital de Argentina?"),
    ("foreign_unrelated", "wie funktioniert ein Elektromotor"),
]
THRESHOLDS = [0.3, 0.35, 0.4, 0.45, 0.5, 0.55, 0.6, 0.65, 0.7]


def main() -> None:
    settings = get_settings()
    embedder = FastEmbedEmbedder(settings)
    docs = np.array(embedder.embed_dense(MEMORIES))

    rel_scores: dict[str, list[float]] = {}
    rel_top1: dict[str, list[bool]] = {}
    for group, query, target in RELEVANT:
        scores = docs @ np.array(embedder.embed_query_dense(query))
        rel_scores.setdefault(group, []).append(float(scores[target]))
        rel_top1.setdefault(group, []).append(int(np.argmax(scores)) == target)
    irr_best: dict[str, list[float]] = {}
    for group, query in IRRELEVANT:
        scores = docs @ np.array(embedder.embed_query_dense(query))
        irr_best.setdefault(group, []).append(float(scores.max()))

    all_rel = [s for v in rel_scores.values() for s in v]
    all_irr = [s for v in irr_best.values() for s in v]
    rows: list[dict[str, Any]] = []
    for t in THRESHOLDS:
        rows.append(
            {
                "threshold": t,
                "recall_relevant": sum(s >= t for s in all_rel) / len(all_rel),
                "false_retrieval_irrelevant": sum(s >= t for s in all_irr) / len(all_irr),
                "recall_by_group": {
                    g: sum(s >= t for s in v) / len(v) for g, v in rel_scores.items()
                },
                "false_by_group": {g: sum(s >= t for s in v) / len(v) for g, v in irr_best.items()},
            }
        )

    def spread(values: list[float]) -> list[float]:
        return [min(values), float(np.median(values)), max(values)]

    result: dict[str, Any] = {
        "dataset_version": DATASET_VERSION,
        "model": settings.embedding_model,
        "current_threshold": settings.memory_min_score,
        "memories": len(MEMORIES),
        "relevant_queries": len(RELEVANT),
        "irrelevant_queries": len(IRRELEVANT),
        "top1_accuracy_relevant": sum(sum(v) for v in rel_top1.values()) / len(RELEVANT),
        "top1_accuracy_by_group": {g: sum(v) / len(v) for g, v in rel_top1.items()},
        "relevant_score_min_median_max": spread(all_rel),
        "relevant_score_by_group": {g: spread(v) for g, v in rel_scores.items()},
        "irrelevant_best_score_min_median_max": spread(all_irr),
        "irrelevant_best_score_by_group": {g: spread(v) for g, v in irr_best.items()},
        "thresholds": rows,
    }
    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / "memory_threshold.json").write_text(json.dumps(result, indent=2), encoding="utf-8")

    rel_groups, irr_groups = list(rel_scores), list(irr_best)
    lines = [
        f"# Memory relevance threshold (real model, hand-written set `{DATASET_VERSION}`)",
        "",
        f"Model `{result['model']}`; {result['memories']} memories, {result['relevant_queries']} "
        f"relevant and {result['irrelevant_queries']} irrelevant queries. Current default "
        f"`MEMORY_MIN_SCORE={result['current_threshold']}`. Top-1 accuracy on the relevant queries "
        f"(is the target memory the best match?): {result['top1_accuracy_relevant']:.2f}; by group: "
        + ", ".join(f"{g} {a:.2f}" for g, a in result["top1_accuracy_by_group"].items())
        + ".",
        "",
        "Cosine score of the target memory (min / median / max): "
        + "; ".join(
            f"{g} {v[0]:.2f} / {v[1]:.2f} / {v[2]:.2f}"
            for g, v in result["relevant_score_by_group"].items()
        )
        + ". Best score any memory gets for an irrelevant query: "
        + "; ".join(
            f"{g} {v[0]:.2f} / {v[1]:.2f} / {v[2]:.2f}"
            for g, v in result["irrelevant_best_score_by_group"].items()
        )
        + ".",
        "",
        "## Overall",
        "",
        "| threshold | relevant queries recalled | irrelevant queries that retrieve something |",
        "|---|---|---|",
    ]
    for r in rows:
        lines.append(
            f"| {r['threshold']:.2f} | {r['recall_relevant']:.2f} | {r['false_retrieval_irrelevant']:.2f} |"
        )
    lines += ["", "## Recall of relevant queries by group", ""]
    lines += ["| threshold | " + " | ".join(rel_groups) + " |", "|---|" + "---|" * len(rel_groups)]
    for r in rows:
        lines.append(
            f"| {r['threshold']:.2f} | "
            + " | ".join(f"{r['recall_by_group'][g]:.2f}" for g in rel_groups)
            + " |"
        )
    lines += ["", "## False retrieval of irrelevant queries by group", ""]
    lines += ["| threshold | " + " | ".join(irr_groups) + " |", "|---|" + "---|" * len(irr_groups)]
    for r in rows:
        lines.append(
            f"| {r['threshold']:.2f} | "
            + " | ".join(f"{r['false_by_group'][g]:.2f}" for g in irr_groups)
            + " |"
        )
    lines += [
        "",
        "HONEST LIMITS: hand-written by one author, debatable near-miss labels, an English embedding "
        "model on a few non-English queries; use it to choose a default, not as a benchmark.",
    ]
    (REPORTS / "memory_threshold.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
