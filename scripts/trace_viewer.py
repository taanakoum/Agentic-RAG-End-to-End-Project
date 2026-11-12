"""Print the traces written to OTEL_TRACES_FILE as readable trees (standard library only).

    python scripts/trace_viewer.py                      # the 3 most recent traces
    python scripts/trace_viewer.py --last 10
    python scripts/trace_viewer.py --trace 3fa9c1       # a trace id (or its first characters)
    python scripts/trace_viewer.py --request-id 6beaae  # the X-Request-ID of a response
    python scripts/trace_viewer.py --slow 500           # only traces slower than 500 ms
    python scripts/trace_viewer.py --min-ms 5 --all-attrs

Enable the file first (.env):  OTEL_ENABLED=true  and  OTEL_TRACES_FILE=data/logs/traces.jsonl
Spans never contain prompts, answers, passages, file names or e-mail addresses: what you see is
structure, timings, counts, model names and error types.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FILE = ROOT / "data" / "logs" / "traces.jsonl"

# attributes worth showing by default; --all-attrs shows everything
INTERESTING = (
    "app.request_id",
    "http.status_code",
    "llm.model",
    "llm.cache",
    "llm.outcome",
    "agent.route",
    "retrieval.mode",
    "ingestion.chunks",
    "ingestion.pages",
    "ingestion.parser",
    "job.outcome",
    "error.type",
    "app.degraded",
)

Span = dict[str, Any]


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def load(path: Path) -> list[Span]:
    spans: list[Span] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            spans.append(json.loads(line))
        except ValueError:  # a half-written line from a process that was killed
            continue
    return spans


def clean(identifier: str | None) -> str:
    return (identifier or "").removeprefix("0x")


def duration_ms(span: Span) -> float:
    return (_time(span["end_time"]) - _time(span["start_time"])).total_seconds() * 1000


def group(spans: list[Span]) -> dict[str, list[Span]]:
    traces: dict[str, list[Span]] = defaultdict(list)
    for span in spans:
        traces[clean(span["context"]["trace_id"])].append(span)
    return traces


def trace_start(spans: list[Span]) -> datetime:
    return min(_time(s["start_time"]) for s in spans)


def trace_duration_ms(spans: list[Span]) -> float:
    end = max(_time(s["end_time"]) for s in spans)
    return (end - trace_start(spans)).total_seconds() * 1000


def request_id_of(spans: list[Span]) -> str:
    for span in spans:
        attributes: dict[str, Any] = span.get("attributes") or {}
        value = attributes.get("app.request_id")
        if value:
            return str(value)
    return ""


def render_trace(
    trace_id: str, spans: list[Span], *, min_ms: float = 0.0, all_attrs: bool = False
) -> str:
    children: dict[str, list[Span]] = defaultdict(list)
    ids = {clean(s["context"]["span_id"]) for s in spans}
    roots: list[Span] = []
    for span in spans:
        parent = clean(span.get("parent_id"))
        if parent and parent in ids:
            children[parent].append(span)
        else:
            roots.append(span)  # a real root, or a child whose parent was not exported
    lines = [
        f"trace {trace_id}  {trace_duration_ms(spans):8.1f} ms  "
        f"{len(spans)} spans  request_id={request_id_of(spans) or '-'}"
    ]

    def walk(span: Span, depth: int) -> None:
        took = duration_ms(span)
        if took >= min_ms or depth == 0:
            attrs: dict[str, Any] = span.get("attributes") or {}
            shown: dict[str, Any] = (
                attrs if all_attrs else {k: attrs[k] for k in INTERESTING if k in attrs}
            )
            resource: dict[str, Any] = span.get("resource") or {}
            resource_attrs: dict[str, Any] = resource.get("attributes") or {}
            service = str(resource_attrs.get("service.name", ""))
            failed = span.get("status", {}).get("status_code") == "ERROR"
            note = "  ".join(f"{k}={v}" for k, v in shown.items())
            lines.append(
                f"  {'  ' * depth}{span['name']}  {took:.1f} ms"
                f"{'  [ERROR]' if failed else ''}"
                f"{'  (' + service + ')' if depth == 0 and service else ''}"
                f"{'  ' + note if note else ''}"
            )
        for child in sorted(children.get(clean(span["context"]["span_id"]), []), key=_start):
            walk(child, depth + 1)

    for root in sorted(roots, key=_start):
        walk(root, 0)
    return "\n".join(lines)


def _start(span: Span) -> datetime:
    return _time(span["start_time"])


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--file", type=Path, default=DEFAULT_FILE)
    parser.add_argument("--last", type=int, default=3, help="how many recent traces")
    parser.add_argument("--trace", help="a trace id or a prefix of it")
    parser.add_argument("--request-id", help="the X-Request-ID of a response")
    parser.add_argument("--slow", type=float, default=0.0, help="only traces slower than N ms")
    parser.add_argument("--min-ms", type=float, default=0.0, help="hide spans shorter than N ms")
    parser.add_argument("--all-attrs", action="store_true")
    args = parser.parse_args()

    if not args.file.exists():
        print(f"No trace file at {args.file}.")
        print("Set OTEL_ENABLED=true and OTEL_TRACES_FILE=data/logs/traces.jsonl in .env,")
        print("restart the stack and use the app.")
        return 1
    traces = group(load(args.file))
    chosen = [
        (tid, spans)
        for tid, spans in traces.items()
        if (not args.trace or tid.startswith(args.trace.removeprefix("0x")))
        and (not args.request_id or request_id_of(spans).startswith(args.request_id))
        and trace_duration_ms(spans) >= args.slow
    ]
    chosen.sort(key=lambda item: trace_start(item[1]))
    if not (args.trace or args.request_id):
        chosen = chosen[-args.last :]
    if not chosen:
        print("No matching traces.")
        return 1
    print(
        "\n\n".join(
            render_trace(t, s, min_ms=args.min_ms, all_attrs=args.all_attrs) for t, s in chosen
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
