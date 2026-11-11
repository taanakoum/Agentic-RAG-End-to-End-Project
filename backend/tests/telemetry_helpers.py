"""In-memory OpenTelemetry capture for tests (installed once per test process)."""

from __future__ import annotations

import json
from typing import Any

from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

EXPORTER = InMemorySpanExporter()
READER = InMemoryMetricReader()


def install(settings: Any) -> None:
    from app.core.telemetry import setup_telemetry

    setup_telemetry(
        settings, span_exporter=EXPORTER, metric_reader=READER, instrument_libraries=True
    )


def spans() -> list[ReadableSpan]:
    return list(EXPORTER.get_finished_spans())


def named(name: str) -> list[ReadableSpan]:
    return [s for s in spans() if s.name == name]


def trace_id_of(span: ReadableSpan) -> str:
    assert span.context is not None
    return f"{span.context.trace_id:032x}"


def serialized() -> str:
    """Every name, attribute and event of every span as one searchable string."""
    out: list[dict[str, Any]] = []
    for s in spans():
        out.append(
            {
                "name": s.name,
                "attributes": dict(s.attributes or {}),
                "events": [(e.name, dict(e.attributes or {})) for e in s.events],
                "status": str(s.status.description or ""),
            }
        )
    return json.dumps(out, default=str)


def metric_points(name: str) -> list[Any]:
    data: Any = READER.get_metrics_data()
    points: list[Any] = []
    if data is None:
        return points
    resources: list[Any] = list(data.resource_metrics)
    for resource in resources:
        scopes: list[Any] = list(resource.scope_metrics)
        for scope in scopes:
            metrics: list[Any] = list(scope.metrics)
            for metric in metrics:
                if metric.name == name:
                    points.extend(list(metric.data.data_points))
    return points
