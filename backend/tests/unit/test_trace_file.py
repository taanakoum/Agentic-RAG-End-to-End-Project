"""The local JSONL trace file: written by the exporter, readable by scripts/trace_viewer.py."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExportResult

from app.core.telemetry import JsonlSpanExporter, traces_path

VIEWER = Path(__file__).resolve().parents[3] / "scripts" / "trace_viewer.py"


def load_viewer() -> ModuleType:
    spec = importlib.util.spec_from_file_location("trace_viewer", VIEWER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["trace_viewer"] = module
    spec.loader.exec_module(module)
    return module


def write_trace(path: Path) -> None:
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(JsonlSpanExporter(path)))
    tracer = provider.get_tracer("test")
    with tracer.start_as_current_span("POST /api/v1/chat") as root:
        root.set_attribute("app.request_id", "req-123")
        with (
            tracer.start_as_current_span("agent.generate"),
            tracer.start_as_current_span("llm.call") as call,
        ):
            call.set_attribute("llm.model", "demo-model")
            call.set_attribute("llm.cache", "miss")
    with tracer.start_as_current_span("task.process_document"):
        pass


def test_the_exporter_appends_one_complete_json_line_per_span(tmp_path: Path) -> None:
    path = tmp_path / "logs" / "traces.jsonl"  # the directory is created on demand
    write_trace(path)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 4
    spans = [json.loads(line) for line in lines]
    assert {s["name"] for s in spans} == {
        "POST /api/v1/chat",
        "agent.generate",
        "llm.call",
        "task.process_document",
    }
    write_trace(path)  # appending, never truncating
    assert len(path.read_text(encoding="utf-8").splitlines()) == 8


def test_an_unwritable_path_never_breaks_the_caller(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    exporter = JsonlSpanExporter(blocker / "child.jsonl")  # a file where a directory is needed
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    with provider.get_tracer("t").start_as_current_span("still works"):
        pass  # the export failed silently; the request is unaffected
    assert exporter.export([]) is SpanExportResult.SUCCESS  # nothing to write: no file access
    assert exporter.shutdown() is None


def test_relative_paths_are_relative_to_the_repository() -> None:
    assert traces_path("data/logs/traces.jsonl").is_absolute()
    assert traces_path(str(Path(__file__).resolve())) == Path(__file__).resolve()


def test_the_viewer_prints_each_trace_as_a_tree_with_timings(tmp_path: Path) -> None:
    path = tmp_path / "traces.jsonl"
    write_trace(path)
    viewer = load_viewer()
    traces = viewer.group(viewer.load(path))
    assert len(traces) == 2
    chat = next(spans for spans in traces.values() if len(spans) == 3)
    text = viewer.render_trace("abc", chat)
    lines = text.splitlines()
    assert "request_id=req-123" in lines[0] and "3 spans" in lines[0]
    assert lines[1].lstrip().startswith("POST /api/v1/chat")
    assert lines[2].startswith("    ") and "agent.generate" in lines[2]  # indented under the root
    assert lines[3].startswith("      ") and "llm.call" in lines[3]
    assert "llm.model=demo-model" in lines[3] and "llm.cache=miss" in lines[3]
    assert "ms" in lines[1]


def test_the_viewer_survives_a_truncated_line_and_filters(tmp_path: Path) -> None:
    path = tmp_path / "traces.jsonl"
    write_trace(path)
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"name": "half written')  # a process was killed mid-write
    viewer = load_viewer()
    spans = viewer.load(path)
    assert len(spans) == 4
    traces = viewer.group(spans)
    request = next(s for s in traces.values() if viewer.request_id_of(s) == "req-123")
    assert viewer.request_id_of(next(iter(traces.values()))) in {"req-123", ""}
    assert viewer.render_trace("t", request, min_ms=1e9).count("\n") == 1  # only the root header
