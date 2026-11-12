"""Routing and rewrite-gate evaluation: no model, no network, deterministic.

Reproduce:

    $env:PYTHONPATH = "backend;."; uv run --no-sync python evaluation/agent/routing_eval.py

Scores the PRODUCTION `classify` / `needs_rewrite` against the hand-labelled dataset
evaluation/datasets/routing_v1.json. Every mismatch is listed in the report (nothing is hidden).
The dataset is small and written by the same author as the rules: it checks the intended
behaviour and guards against regressions, it is not an estimate of accuracy on real traffic.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from app.agents.routing import classify, needs_rewrite

ROOT = Path(__file__).resolve().parents[2]
DATASET = ROOT / "evaluation" / "datasets" / "routing_v1.json"
REPORTS = ROOT / "evaluation" / "reports"
ROUTES = ("smalltalk", "clarify", "kb")


def evaluate(dataset: dict[str, Any]) -> dict[str, Any]:
    confusion: Counter[tuple[str, str]] = Counter()
    wrong: list[dict[str, str]] = []
    for case in dataset["route_cases"]:
        got = classify(case["message"]).route
        confusion[(case["route"], got)] += 1
        if got != case["route"]:
            wrong.append({"message": case["message"], "expected": case["route"], "got": got})
    per_route: dict[str, dict[str, float | int]] = {}
    for route in ROUTES:
        tp = confusion[(route, route)]
        fn = sum(confusion[(route, other)] for other in ROUTES if other != route)
        fp = sum(confusion[(other, route)] for other in ROUTES if other != route)
        per_route[route] = {
            "support": tp + fn,
            "precision": tp / (tp + fp) if tp + fp else 1.0,
            "recall": tp / (tp + fn) if tp + fn else 1.0,
        }
    total = len(dataset["route_cases"])
    rewrite_wrong: list[dict[str, Any]] = []
    for case in dataset["rewrite_cases"]:
        got = needs_rewrite(case["message"], has_history=case["has_history"])
        if got != case["needs_rewrite"]:
            rewrite_wrong.append({**case, "got": got})
    return {
        "dataset": dataset["version"],
        "route_cases": total,
        "route_accuracy": (total - len(wrong)) / total,
        "per_route": per_route,
        "confusion": {f"{a}->{b}": n for (a, b), n in sorted(confusion.items())},
        "route_mismatches": wrong,
        "rewrite_cases": len(dataset["rewrite_cases"]),
        "rewrite_accuracy": 1 - len(rewrite_wrong) / len(dataset["rewrite_cases"]),
        "rewrite_mismatches": rewrite_wrong,
    }


def render(result: dict[str, Any]) -> str:
    lines = [
        "# Routing evaluation",
        "",
        f"Dataset `{result['dataset']}`: {result['route_cases']} route cases, "
        f"{result['rewrite_cases']} rewrite-gate cases. No model is called.",
        "",
        f"**Route accuracy: {result['route_accuracy']:.1%}** · "
        f"rewrite-gate accuracy: {result['rewrite_accuracy']:.1%}",
        "",
        "| route | support | precision | recall |",
        "|---|---|---|---|",
    ]
    for route, row in result["per_route"].items():
        lines.append(
            f"| {route} | {row['support']} | {row['precision']:.2f} | {row['recall']:.2f} |"
        )
    confusion = ", ".join(f"{k}: {v}" for k, v in result["confusion"].items())
    lines += ["", f"Confusion (expected->got): {confusion}"]
    for title, key in (
        ("Route mismatches", "route_mismatches"),
        ("Rewrite mismatches", "rewrite_mismatches"),
    ):
        lines += ["", f"## {title}", ""]
        items = result[key]
        lines += [f"- `{json.dumps(item, ensure_ascii=False)}`" for item in items] or ["None."]
    lines += [
        "",
        "Limits: small hand-written set, same author as the rules; it guards intended behaviour "
        "(notably that small-talk look-alikes with a real request go to the knowledge base).",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    result = evaluate(json.loads(DATASET.read_text(encoding="utf-8")))
    REPORTS.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(result, indent=1, ensure_ascii=False) + "\n"
    (REPORTS / "routing_eval.json").write_text(payload, encoding="utf-8")
    (REPORTS / "routing_eval.md").write_text(render(result), encoding="utf-8")
    print(render(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
