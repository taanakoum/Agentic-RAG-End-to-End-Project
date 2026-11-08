# 07 Testing, Evaluation, Observability, Load, Docker, Deployment

## Testing (Pytest; unit / integration / e2e / security / concurrency / performance)
Core tests must not need paid LLM calls (mock LLM only; real PG, Redis, Qdrant, Celery, storage in integration/E2E; Testcontainers or Docker services). Deterministic where possible. Provide one command per category plus full suite.
Unit: hashing, validation, JWT, refresh, duplicate prevention, authz, PDF validation/parsing, chunking, dedup, embedding adapters, sparse/dense, fusion, rerank, citation verification, RAGResponse validation, agent nodes, graph transitions, memory, task logic, exception handlers.
Integration: API+PG, migrations, Celery+Redis, Qdrant indexing, retrieval, rerank, checkpoint persistence, storage, ingestion, session + memory persistence, citation verification.
E2E scenarios: A registration; B valid PDF upload through indexing; C invalid PDFs (missing, empty, wrong ext, renamed non-PDF, corrupted, oversized, encrypted, scanned, duplicate); D RAG chat with citations + reopen; E multi-agent bounded run; F memory across sessions + deletion; G two-user isolation incl. manipulated thread IDs; H concurrent sessions (separate, same-conversation, duplicate submit, simultaneous streams, delete during run); I SSE ordering/tokens/citations/errors/completion/cancellation/persistence.
Security: SQLi, unauthorized/cross-user access, token misuse, expired tokens, refresh reuse, brute force, malformed PDFs, path traversal, prompt injection, tool access, data leakage.
Failure recovery: Redis/PG/Qdrant interruption, worker crash, provider timeout, storage error, client disconnect.
Quality commands: `ruff check .`, `ruff format --check .`, `pyright` (or mypy), `pytest`, migration run, Docker smoke test. Never suppress errors to go green.

## Evaluation (reproducible, versioned datasets; never fabricate ground truth/results)
Dataset: questions, ground-truth answers, relevant docs/chunks, expected evidence, variations.
Retrieval: Recall@K, Precision@K, HitRate@K, MRR, NDCG for dense / sparse / hybrid / hybrid+RRF / hybrid+rerank.
Generation: correctness, faithfulness, context relevance/precision/recall, citation precision/correctness, completeness, hallucination rate. LLM-as-judge used cautiously plus deterministic checks and human-labelled data.
Agent: retrieval-necessity classification, tool selection, routing, termination, structured-output validity, evidence selection, collaboration, failure recovery.
Performance: end-to-end/retrieval/fusion/rerank/LLM latency, tokens, cost, throughput, error rate.
Configurations: Baseline (dense, no rerank, single); Hybrid (dense+sparse, single); Reranked (single); Multi-agent (hybrid+rerank, multi); Advanced (best evaluated). Record model versions, dataset version, retrieval params. Generate reproducible reports in `evaluation/reports/`. Decide chunking by evaluation.

## Observability
OpenTelemetry + Langfuse/Phoenix (verify compatibility). Trace API requests, graph/node execution, routing, retrieval + scores, fusion, rerank, LLM calls, tokens, cost, latency, errors, Celery jobs, DB. Structured logs with correlation/request IDs propagated Streamlit -> FastAPI -> LangGraph -> PG/Qdrant/Redis -> Celery -> providers. Health checks for API, PG, Redis, Qdrant, Celery workers, storage, optionally provider. Redaction + retention. Must be able to trace a failed upload or failed RAG request across services without exposing private data.

## Load testing (Locust or k6)
Levels: 1, 10, 25, 50 users (test levels, not promised capacity). Simulate auth, uploads, polling, doc retrieval, chat, streaming, multi-agent, history. Measure RPS, success/error rate, median/P95/P99, streaming latency, active SSE, DB connections, Redis, Celery queue length, worker memory/CPU, LLM throttling, job duration. Also: slow provider, rate limits, DB/Redis/Qdrant failure, worker restart, large uploads, duplicate requests. Report real numbers with hardware/config/limitations; identify first bottlenecks; fix defects found. No scalability claims without measurements.

## Docker
Dockerfiles + Compose: backend, Celery worker, Beat, Streamlit, PostgreSQL, Redis, Qdrant, observability where practical. Health checks, depends_on conditions, persistent volumes, env config, migration procedure, restart policies, resource limits, non-root, dev/prod config split, graceful shutdown. `docker compose up --build` starts the stack from clean; data survives restarts.

## Deployment (free tier, honest)
Verify CURRENT provider terms (sleep behavior, ephemeral FS, quotas, CPU/RAM, worker support, timeouts, connection limits, backups) for app hosting, workers, PG, Redis, Qdrant, object storage, frontend, observability. Distinguish: (1) full local Docker, (2) free hosted demo, (3) features needing paid infra. Never claim "free" if an essential component costs money; never silently lose persistent data. Do NOT create accounts, spend money, or deploy without user authorization: prepare config, verify locally, document env vars/migrations/storage/security/commands/health checks/rollback, state what was not deployed. URL only after verified.

## Additional features (evaluate, do not pad)
Query classification/rewriting/expansion, compression, hallucination detection, multiple KBs, retrieval/semantic caching, model fallback, cost tracking, quotas, data export/deletion, prompt versioning, audit logs. Implement by value/complexity; list deferred ones in KNOWN_ISSUES.md.
