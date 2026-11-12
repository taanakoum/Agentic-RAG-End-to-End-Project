# Observability

OpenTelemetry traces and metrics, structured logs that carry the trace id, health endpoints.
Everything is off by default and costs nothing unless enabled; no vendor is required.

## Turning it on

Set in `.env` (see `docs/DEPLOYMENT.md` for the full list):

| Variable | Meaning |
|---|---|
| `OTEL_ENABLED=true` | export spans (and metrics if enabled) |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | OTLP/HTTP base URL, e.g. `http://127.0.0.1:6006` (Arize Phoenix), an OpenTelemetry Collector, or a Langfuse OTLP endpoint. `/v1/traces` and `/v1/metrics` are appended |
| `OTEL_EXPORTER_OTLP_HEADERS` | JSON object, e.g. `{"authorization": "Bearer ..."}` |
| `OTEL_SAMPLE_RATIO` | 0..1, parent-based (a trace is kept or dropped as a whole, also in the worker) |
| `OTEL_METRICS_ENABLED` | also export metrics every 30 s |
| `OTEL_INSTRUMENT_LIBRARIES` | auto-instrument SQLAlchemy, Redis, httpx |

Both the API and each Celery worker process call the same setup (`app/core/telemetry.py`), with
service names `agentic-rag-api` and `agentic-rag-worker`.

## What a trace looks like

One trace per user request, across processes:

```
POST /api/v1/chat                      (FastAPI server span, attribute app.request_id)
└─ chat.turn
   ├─ agent.classify -> agent.prepare_query -> agent.retrieve -> agent.generate
   │     retrieval.search / retrieval.rerank        llm.call (llm.model, llm.cache, tokens)
   └─ agent.check_output -> agent.finalize -> agent.persist
POST /api/v1/documents/upload
└─ (outbox row stores the traceparent) ... task.process_document   (worker, same trace id)
      ├─ ingestion.parse   (format, pages, chunks, parser, ocr pages)
      ├─ ingestion.embed
      └─ ingestion.index
```

SQLAlchemy, Redis and httpx (model provider) spans appear as children automatically.

Correlation: every response carries `X-Request-ID` and `X-Trace-Id`; the server span has
`app.request_id`; every log line has `request_id`, `trace_id` and `span_id`. A user quoting either id
in a bug report finds the trace and the logs.

Background work: the transactional outbox stores the W3C `traceparent` of the request that
caused an event; the dispatcher sends it as a Celery header and the task continues that trace
(`app/tasks/tracing.py`). Tested with a real worker, including a permanently failing document.

## Metrics (names)

`llm.requests`, `llm.tokens`, `llm.latency`, `llm.cache`, `chat.turns`, `chat.turn.duration`,
`retrieval.duration`, `ingestion.stage.duration`. Labels are low-cardinality (model, outcome, cache
status, route, retrieval mode and stage, ingestion stage and parser); never user or document ids.

## Privacy rules (enforced in code and tested)

- No prompts, answers, questions, chunk text, quotes, file names, e-mails, tokens or memory ever go
  into a span attribute or event: keys matching the deny-list are dropped whatever the value, values
  that look like e-mails, bearer tokens or API keys are replaced by `[redacted]`, strings are cut at
  200 characters, and non-scalar values are dropped (`safe_attributes`).
- Failed spans record only the exception TYPE, never its message.
- The user appears only as a hash (`app.user`). Redis queries are sanitized; SQL parameters are not
  recorded; HTTP headers and bodies are not recorded.
- Tests: `tests/unit/test_telemetry.py` (rules), `tests/integration/test_observability.py` (a whole
  chat turn and an upload, scanning every exported span for the question, answer, passage text,
  file name, e-mail, bearer token and user id), `tests/unit/test_otlp_export.py` (real OTLP
  export to a local receiver: the auth header arrives, the denied attribute does not).
- Retention is the backend's job (Phoenix/Langfuse/collector settings); because spans contain no
  user content, a short or long retention does not change the privacy posture. Logs follow the
  existing redaction processor.

## Health checks

`GET /api/v1/health/live`, `/ready` (PostgreSQL, Redis, Qdrant; no details leaked),
`/workers` (Celery workers answer) and `/llm` (configured model chain, today's quota usage and
reset time). Health probes are
excluded from tracing.

## Trying it locally

Any OTLP/HTTP receiver works. With Arize Phoenix installed in its own virtual environment
(not part of this project's lockfile): start `phoenix serve`, then set `OTEL_ENABLED=true` and
`OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:6006`. NOT verified against Phoenix or Langfuse in
this project (KI-037); the export path is verified against a local OTLP receiver.
