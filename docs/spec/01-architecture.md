# 01 Architecture

## Stack (preferred; pin versions from current official docs, commit a lockfile)
FastAPI (async), LangGraph, LangChain, Pydantic v2, Pyright or mypy, PostgreSQL, SQLAlchemy (async) + Alembic, Qdrant (dense + sparse), RRF fusion, cross-encoder reranker, Celery + Redis, JWT, Argon2id, Streamlit, RAGAS/DeepEval (or justified alternative), OpenTelemetry + Langfuse/Phoenix, Pytest, Locust or k6, Docker + Compose, Ruff.
Extra tech allowed only with documented rationale.

## Components
Streamlit frontend; versioned FastAPI backend; auth/authz; services + repositories; LangGraph engine (single + multi); ingestion pipeline; dense/sparse retrieval; cross-encoder reranking; LLM/embedding/reranker provider abstractions; PostgreSQL; Qdrant; Redis + Celery; persistent memory; sessions/checkpoints; evaluation; observability; object storage; security + concurrency control.
Business logic is independent of HTTP handlers and the frontend. Use DI and interfaces only where they add value.

## Two mandatory, separate pipelines
### Interactive (never via Celery)
Streamlit -> async FastAPI routers -> auth -> services -> LangGraph -> (hybrid retrieval -> rerank | agent tools) -> context construction -> LLM -> citation verification -> Pydantic validation -> SSE -> Streamlit.
Requirements: async I/O; async LangGraph; SSE token/event streaming; timeouts + cancellation; persist messages + final responses; bounded concurrency for LLM/retrieval; handle client disconnects; never block the event loop with CPU work (offload to executor/threadpool or worker); concurrent conversations must not interfere.

### Background (Celery)
Streamlit -> documents router -> auth/ownership -> PDF validation -> object storage -> PostgreSQL document + job record (+ outbox event, same transaction) -> dispatcher publishes -> Redis -> Celery worker: parse + metadata -> chunk -> embed -> sparse -> Qdrant -> PostgreSQL status -> status API -> UI.
Celery also for: OCR, re-indexing, bulk indexing, memory consolidation, periodic cleanup/reconciliation, evaluation jobs.
Requirements: return document_id + job_id immediately; persisted states; status endpoints; retries with backoff; failed-task recovery; idempotent tasks; duplicate delivery safe; execution timeouts; configurable worker concurrency and resource limits; workers verify document ownership.

### Celery/asyncio boundary
Do not sprinkle `asyncio.run()` or create unmanaged loops/clients. Define one explicit execution model (e.g. sync Celery tasks using sync clients, or one managed loop per worker process with lifecycle hooks). Document it in ARCHITECTURE_DECISIONS.md and test it.

## Infrastructure responsibilities
- PostgreSQL (source of truth): users, refresh sessions, documents, versions, jobs, outbox, conversations, messages, LangGraph checkpoints, memories, preferences, feedback, evaluation results, audit events.
- Qdrant: chunk dense vectors, sparse vectors, filter metadata, optional memory vectors.
- Redis: Celery broker, cache, rate limiting, short-lived coordination. Never the only copy of critical data.
- Object storage: original PDFs, durable processing artifacts. Abstraction with local-volume (dev) and S3-compatible (hosted) adapters.
Document the consistency strategy across all four.

## Stateless backend / scale
No mutable global user state; no process-local sessions; shared services only; DB connection pooling; concurrency limits, timeouts, backpressure, graceful shutdown. Architecture must permit horizontal scaling.

## Resource management
Configurable max concurrent LLM calls, embedding batch limits, rerank batch/timeouts, per-user rate limits and quotas, Celery concurrency, retrieval candidate caps, token limits, max active streams. Graceful behavior on provider rate limits/outages.

## Provider abstraction
Interfaces for chat, embeddings, reranking. Config-driven model switching. Timeouts, retries, rate-limit handling, structured output, streaming, token + cost accounting, model metadata, provider health. Hosted provider + local/OpenAI-compatible option where practical. Mock providers for tests only (clearly separate test adapters). No hardcoded credentials.

## Target repo layout
```
agentic-rag-platform/
  backend/app/{main.py, api/v1/{router.py,routers/{auth,users,documents,ingestion,chat,sessions,memory,feedback,evaluation,health}.py}, core/{config,security,logging,exceptions}.py, dependencies/, database/{session,base}.py, models/, schemas/, repositories/, services/, ingestion/, storage/, retrieval/, reranking/, agents/, memory/, tasks/, evaluation/, observability/}
  backend/migrations/
  backend/tests/{unit,integration,e2e,security,concurrency}/
  frontend/streamlit_app/
  evaluation/{datasets,scripts,reports}/
  deployment/{docker,monitoring,hosting}/
  load_testing/  docs/  scripts/
  docker-compose.yml  .env.example  .gitignore  pyproject.toml  README.md  <lockfile>
```
Adjust only with justification. No circular imports, no unexplained type-ignore, no hardcoded secrets.

## Required docs
ARCHITECTURE.md (with system diagram + sequence diagrams for ingestion and query), IMPLEMENTATION_PLAN.md, ARCHITECTURE_DECISIONS.md, SECURITY.md, DATA_MODEL.md, API_SPECIFICATION.md, TESTING_STRATEGY.md, DEPLOYMENT.md.
