# Observability

OpenTelemetry traces and metrics, structured logs that carry the trace id, health endpoints.
Everything is off by default and costs nothing unless enabled; no vendor is required.

## Turning it on

Set in `.env` (see `docs/DEPLOYMENT.md` for the full list):

| Variable | Meaning |
|---|---|
| `OTEL_ENABLED=true` | export spans (and metrics if enabled) |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | OTLP/HTTP base URL, e.g. `http://127.0.0.1:6006` (Arize Phoenix), an OpenTelemetry Collector, or a Langfuse OTLP endpoint. `/v1/traces` and `/v1/metrics` are appended |
| `OTEL_TRACES_FILE` | also (or only) append every finished span as one JSON line to this file, e.g. `data/logs/traces.jsonl` (relative to the repository root); read it with `scripts/trace_viewer.py`, no vendor needed |
| `OTEL_EXPORTER_OTLP_HEADERS` | JSON object, e.g. `{"authorization": "Bearer ..."}` |
| `OTEL_SAMPLE_RATIO` | 0..1, parent-based (a trace is kept or dropped as a whole, also in the worker) |
| `OTEL_METRICS_ENABLED` | also export metrics every 30 s |
| `OTEL_INSTRUMENT_LIBRARIES` | auto-instrument SQLAlchemy, Redis, httpx |

Both the API and each Celery worker process call the same setup (`app/core/telemetry.py`), with
service names `agentic-rag-api` and `agentic-rag-worker`.

## How to look at it (quick guide)

There are three things to look at: **logs**, **traces** and **health/quota endpoints**.

**1. Logs** (always on). Every line of the API, worker and beat is structured and carries
`request_id` (and `trace_id`/`span_id` once tracing is on):

```bash
uv run --no-sync python scripts/stack.py logs api        # also: worker, beat, ui
```

A response header `X-Request-ID` (and `X-Trace-Id`) lets you jump from "this request felt slow" to the
exact log lines: search the log for that id.

**2. Traces without any extra software** (a local file). In `.env`:

```
OTEL_ENABLED=true
OTEL_TRACES_FILE=data/logs/traces.jsonl
```

Restart (`stack.py stop`, `stack.py start`), use the app (upload a document, ask a question), then:

```bash
uv run --no-sync python scripts/trace_viewer.py                # the 3 most recent traces as trees
uv run --no-sync python scripts/trace_viewer.py --last 10 --min-ms 5
uv run --no-sync python scripts/trace_viewer.py --request-id 6beaae   # from the X-Request-ID header
uv run --no-sync python scripts/trace_viewer.py --slow 2000    # only traces slower than 2 s
```

Real output from this project (one question, real model; ids shortened):

```
trace fa02e75...   5512.6 ms  26 spans  request_id=0e382ef...
  POST /api/v1/chat  5512.6 ms  (agentic-rag-api)  http.status_code=200
    chat.turn  5410.8 ms
      agent.retrieve  3637.4 ms
        retrieval.search  3618.8 ms
          retrieval.rerank  3057.9 ms
      agent.generate  1494.8 ms
        llm.call  1492.2 ms  llm.model=gemini-3.5-flash-lite  llm.cache=miss
      agent.check_output  12.6 ms
      agent.persist  14.0 ms
```

and an upload, which continues into the background worker (the same trace):

```
trace 3759520...   25774.9 ms  16 spans
  POST /api/v1/documents/upload  695.8 ms  (agentic-rag-api)  http.status_code=202
    task.process_document  25085.8 ms  job.outcome=completed
      ingestion.parse  24334.6 ms  ingestion.pages=3  ingestion.parser=docling
      ingestion.embed  629.9 ms
      ingestion.index  40.1 ms
```

(The first PDF after a start spends most of its time loading Docling; the re-ranker dominates a
question on a CPU, see KI-023.) Failed spans are marked `[ERROR]` with only the exception *type*.

**3. A trace UI.** Any OTLP/HTTP receiver works. Two free options:

- *Arize Phoenix* (own virtual environment, not part of this project's dependencies):
  `uvx --python 3.12 --from arize-phoenix phoenix serve` -> UI at http://127.0.0.1:6006 (verified with Phoenix 21.0.0), then
  `OTEL_ENABLED=true` and `OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:6006` in `.env`.
- *Any OpenTelemetry Collector / Grafana Tempo / Jaeger*: point `OTEL_EXPORTER_OTLP_ENDPOINT` at its
  OTLP/HTTP port (4318) (`/v1/traces` is appended).

**4. Health and quota** (no setup): `GET /api/v1/health/ready` (PostgreSQL, Redis, Qdrant, storage),
`/health/workers` (Celery answers), `/health/llm` (configured model chain, today's request count against
the budget, time until the quota resets).

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
