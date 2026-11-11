"""OpenTelemetry tracing and metrics, with privacy built in.

Goals (docs/spec/07 "Observability"): trace API requests, agent nodes, retrieval stages, LLM calls
and background jobs, correlate them with request ids and logs, and keep private data OUT.

Privacy rules, enforced in code and tested:
  * spans carry identifiers, counts, timings, model names and status only. Prompts, answers,
    document text, queries, filenames, emails and secrets are NEVER attributes. `safe_attributes`
    drops denied keys, truncates strings and redacts anything that looks like a secret or email;
  * user ids are hashed (`app.user`), never sent raw;
  * library instrumentation is configured not to capture statements or arguments (Redis queries
    are sanitised), and a test scans every span of a full chat turn for private strings.
Export is OFF unless `OTEL_ENABLED=true` (then OTLP/HTTP to `OTEL_EXPORTER_OTLP_ENDPOINT`, which
Arize Phoenix, Langfuse and any OTLP collector accept). When off, the API is a no-op.
"""

from __future__ import annotations

import re
import time
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from typing import Any

from opentelemetry import context as otel_context
from opentelemetry import metrics, propagate, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import MetricReader, PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor, SpanExporter
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
from opentelemetry.trace import Span, SpanKind, Status, StatusCode

from app.core.config import Settings

INSTRUMENTATION = "agentic_rag"
MAX_VALUE_CHARS = 200

# attribute keys that must never be recorded, whatever the caller passes
DENIED_KEYS = re.compile(
    r"(prompt|answer|query|question|message|content|text|excerpt|quote|filename|file_name|email|"
    r"password|secret|token|api[_-]?key|authorization|cookie|memory|history|chunk_text|body)",
    re.IGNORECASE,
)
# values that look like secrets or personal data are replaced even under an allowed key
_VALUE_PATTERNS = (
    re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),  # email
    re.compile(r"\bBearer\s+\S+", re.IGNORECASE),
    re.compile(r"\b(sk|pk|rk|ghp|gho|xox[bp])[-_][A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bAI[a-zA-Z0-9_-]{30,}\b"),  # Google-style keys
    re.compile(r"\bAQ\.[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)
REDACTED = "[redacted]"
_state: dict[str, Any] = {"configured": False, "provider": None}


def _redact_value(value: str) -> str:
    for pattern in _VALUE_PATTERNS:
        value = pattern.sub(REDACTED, value)
    return value[:MAX_VALUE_CHARS]


def safe_attributes(attributes: Mapping[str, Any] | None) -> dict[str, Any]:
    """Only scalars (or lists of them) under non-sensitive keys; strings are redacted/truncated."""
    clean: dict[str, Any] = {}
    for key, value in (attributes or {}).items():
        if DENIED_KEYS.search(key) or value is None:
            continue
        if isinstance(value, bool | int | float):
            clean[key] = value
        elif isinstance(value, str):
            clean[key] = _redact_value(value)
        elif isinstance(value, list | tuple):
            items: list[Any] = []
            for item in value:  # pyright: ignore[reportUnknownVariableType]
                if isinstance(item, str):
                    items.append(_redact_value(item))
                elif isinstance(item, bool | int | float):
                    items.append(item)
            clean[key] = items
    return clean


def tracer() -> trace.Tracer:
    return trace.get_tracer(INSTRUMENTATION)


@contextmanager
def span(name: str, **attributes: Any) -> Generator[Span]:
    """A span with safe attributes. Exceptions are recorded by type only (never the message)."""
    with tracer().start_as_current_span(
        name,
        attributes=safe_attributes(attributes),
        record_exception=False,
        set_status_on_exception=False,
    ) as current:
        try:
            yield current
        except BaseException as exc:
            current.set_attribute("error.type", type(exc).__name__)
            current.set_status(Status(StatusCode.ERROR))
            raise


def set_attributes(**attributes: Any) -> None:
    current = trace.get_current_span()
    if current.is_recording():
        for key, value in safe_attributes(attributes).items():
            current.set_attribute(key, value)


# ------------------------------------------------------------------ correlation


def trace_ids() -> tuple[str, str] | None:
    ctx = trace.get_current_span().get_span_context()
    if not ctx.is_valid:
        return None
    return f"{ctx.trace_id:032x}", f"{ctx.span_id:016x}"


def inject_traceparent() -> str | None:
    """The W3C `traceparent` of the current span (stored in outbox events and task headers)."""
    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    return carrier.get("traceparent")


@contextmanager
def continue_trace(traceparent: str | None) -> Generator[None]:
    """Run the block as a child of a trace started elsewhere (e.g. the API request)."""
    if not traceparent:
        yield
        return
    ctx = propagate.extract({"traceparent": traceparent})
    token = otel_context.attach(ctx)
    try:
        yield
    finally:
        otel_context.detach(token)


def hashed_user(user_id: object) -> str:
    from app.infra.rate_limit import subject_key

    return subject_key(str(user_id))


# ------------------------------------------------------------------ metrics


class Instruments:
    """Lazily created so importing this module never needs a configured provider."""

    def __init__(self) -> None:
        meter = metrics.get_meter(INSTRUMENTATION)
        self.llm_requests = meter.create_counter("llm.requests", description="model calls")
        self.llm_tokens = meter.create_counter("llm.tokens", description="prompt/completion tokens")
        self.llm_latency = meter.create_histogram("llm.latency", unit="ms")
        self.chat_turns = meter.create_counter("chat.turns", description="chat turns by outcome")
        self.chat_latency = meter.create_histogram("chat.turn.duration", unit="ms")
        self.retrieval_latency = meter.create_histogram("retrieval.duration", unit="ms")
        self.ingestion_stage = meter.create_histogram("ingestion.stage.duration", unit="ms")
        self.cache = meter.create_counter("llm.cache", description="cache hit/miss/bypass/error")


_instruments: Instruments | None = None


def instruments() -> Instruments:
    global _instruments
    if _instruments is None:
        _instruments = Instruments()
    return _instruments


@contextmanager
def timed() -> Generator[list[float]]:
    """`with timed() as t: ...` then `t[0]` is the elapsed milliseconds."""
    holder = [0.0]
    started = time.perf_counter()
    try:
        yield holder
    finally:
        holder[0] = (time.perf_counter() - started) * 1000


# ------------------------------------------------------------------ setup


def setup_telemetry(
    settings: Settings,
    *,
    service_name: str | None = None,
    span_exporter: SpanExporter | None = None,
    metric_reader: MetricReader | None = None,
    instrument_libraries: bool | None = None,
) -> bool:
    """Configure tracing/metrics once per process. Returns True if telemetry is active.

    With `span_exporter` / `metric_reader` (tests) no network is involved. Otherwise export
    happens only when OTEL_ENABLED and an endpoint are configured.
    """
    if _state["configured"]:
        return bool(_state["provider"])
    _state["configured"] = True
    wants_export = settings.otel_enabled and bool(settings.otel_exporter_otlp_endpoint)
    if span_exporter is None and not wants_export:
        return False
    resource = Resource.create(
        {
            "service.name": service_name or settings.service_name,
            "service.namespace": "agentic-rag",
            "deployment.environment": settings.app_env,
        }
    )
    provider = TracerProvider(
        resource=resource, sampler=ParentBased(TraceIdRatioBased(settings.otel_sample_ratio))
    )
    if span_exporter is not None:
        provider.add_span_processor(SimpleSpanProcessor(span_exporter))
    else:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        endpoint = settings.otel_exporter_otlp_endpoint.rstrip("/")
        exporter = OTLPSpanExporter(
            endpoint=endpoint if endpoint.endswith("/traces") else f"{endpoint}/v1/traces",
            headers=settings.otel_exporter_otlp_headers or None,
            timeout=5,
        )
        provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    readers: list[MetricReader] = []
    if metric_reader is not None:
        readers.append(metric_reader)
    elif wants_export and settings.otel_metrics_enabled:
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter

        endpoint = settings.otel_exporter_otlp_endpoint.rstrip("/")
        readers.append(
            PeriodicExportingMetricReader(
                OTLPMetricExporter(
                    endpoint=f"{endpoint}/v1/metrics",
                    headers=settings.otel_exporter_otlp_headers or None,
                ),
                export_interval_millis=30_000,
            )
        )
    if readers:
        metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=readers))
    _state["provider"] = provider
    if (
        instrument_libraries
        if instrument_libraries is not None
        else settings.otel_instrument_libraries
    ):
        instrument_libs()
    return True


def instrument_libs() -> None:
    """Auto-instrument HTTP clients, Redis and SQLAlchemy WITHOUT capturing arguments."""
    import importlib

    def load(module: str, name: str) -> Any:
        return getattr(importlib.import_module(module), name)

    load("opentelemetry.instrumentation.httpx", "HTTPXClientInstrumentor")().instrument()
    # cache values and keys stay out of spans
    load("opentelemetry.instrumentation.redis", "RedisInstrumentor")().instrument(
        sanitize_query=True
    )
    load("opentelemetry.instrumentation.sqlalchemy", "SQLAlchemyInstrumentor")().instrument(
        enable_commenter=False
    )


def is_active() -> bool:
    return _state.get("provider") is not None


def instrument_fastapi(app: Any) -> None:
    """Server spans for HTTP requests (health probes excluded; no bodies, no headers)."""
    import importlib

    module = importlib.import_module("opentelemetry.instrumentation.fastapi")
    module.FastAPIInstrumentor.instrument_app(app, excluded_urls="/api/v1/health/.*")


def shutdown_telemetry() -> None:
    provider = _state.get("provider")
    if provider is not None:
        provider.shutdown()


__all__ = [
    "REDACTED",
    "SpanKind",
    "continue_trace",
    "hashed_user",
    "inject_traceparent",
    "instrument_fastapi",
    "instruments",
    "is_active",
    "safe_attributes",
    "set_attributes",
    "setup_telemetry",
    "span",
    "timed",
    "trace_ids",
    "tracer",
]
