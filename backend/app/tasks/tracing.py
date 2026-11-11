"""Trace plumbing for Celery tasks: continue the trace of the request that queued the work."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from typing import Any, cast

from celery import Task
from opentelemetry.trace import Span

from app.core.telemetry import continue_trace, span


def traceparent_of(task: Task[Any, Any]) -> str | None:
    request = task.request
    direct = getattr(request, "traceparent", None)
    if isinstance(direct, str):
        return direct
    headers: Any = getattr(request, "headers", None)
    if isinstance(headers, dict):
        value = cast("dict[str, Any]", headers).get("traceparent")
        return value if isinstance(value, str) else None
    return None


@contextmanager
def task_span(task: Task[Any, Any], name: str, **attributes: Any) -> Generator[Span]:
    """A child span of the originating request's trace (or a fresh root if there is none)."""
    with continue_trace(traceparent_of(task)), span(name, **attributes) as current:
        yield current
