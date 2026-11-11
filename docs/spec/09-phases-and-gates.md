# 09 Phases and Quality Gates

Never advance past a phase until its exit condition is actually verified (or a real blocker is recorded). Verification = commands run, results logged.

| Phase | Scope | Exit condition | Gate |
|---|---|---|---|
| 0 Recon | Inspect repo (pwd, git status/branch/log, find), Python version, deps, docker, env, tests, existing code; check current docs of FastAPI, LangGraph, LangChain, SQLAlchemy, Pydantic, Celery, Qdrant. Produce assessment, target structure, status tracker, exec log, architecture doc, risk register. | Repo understood, work preserved, plan documented | - |
| 1 Foundation | Structure, env, pinned deps + lockfile, Pydantic Settings, PG, SQLAlchemy, Alembic, Redis, Qdrant, storage abstraction, Celery, structured logging, Dockerfiles, Compose, health checks, `.env.example`, pooling/lifecycle. Dev/test/prod config. | Build images, start services, health OK, connect PG/Redis/Qdrant, migrations run, storage + data persist across restart | G1 |
| 2 FastAPI + Auth | App entry, /api/v1 routers, DI, services, repos, schemas, exception handlers, error format, Argon2id, normalized email unique, JWT, refresh rotation, logout/revocation, auth deps, ownership checks, Ruff + Pyright | Auth tests pass incl. concurrent duplicate registration, rotation, expiry, no hash leakage, ownership | G2 |
| 3 Ingestion | Storage adapters, upload API, full PDF validation, parsing/OCR/chunking, outbox + dispatcher, Celery tasks, states, retries, idempotency, SHA-256 dedup, Qdrant indexing interface, status endpoints, deletion | Tests 1-13 (valid, empty, missing, renamed, corrupted, encrypted, oversized, scanned, duplicate, concurrent duplicate, worker crash, retry, container restart) pass; truthful persistent status; deletion removes file + rows + vectors | G3 |
| 4 Retrieval | Embedding abstraction, dense, sparse, RRF, rerank, authz filters, citation metadata, small controlled dataset | Recall/Precision/MRR/NDCG measured for dense, sparse, hybrid, reranked; manual inspection done | G4 |
| 5 Sessions + Memory | Session CRUD, messages, PG checkpointer, id mapping, same-thread locking, idempotency keys, memory CRUD/retrieval/consolidation | History + checkpoint survive restart; independent convs concurrent; conflicts behave as defined; memory crosses sessions; deletion complete; isolation proven | G5 |
| 6 Single-agent | Graph nodes, RAGResponse, bounded repair, citation verification, SSE with typed events, disconnect/timeout/persistence-failure handling | Full real workflow (auth -> upload -> index -> session -> question -> cited, validated, streamed, persisted, reopened) | G6 |
| 7 Multi-agent | ~~Supervisor graph...~~ REMOVED by user decision (2026-11-11): this application uses the single-agent graph only. | n/a | ~~G7~~ n/a |
| 8 Eval + Observability | Datasets, retrieval/generation/citation/agent metrics, perf + cost, OTel + Langfuse/Phoenix, trace correlation, redaction, reports | Evaluations reproducible with documented commands; traces usable | - |
| 9 Frontend | All Streamlit features per 06 | Real frontend-to-backend workflows pass; cannot see other users' data by manipulating state | G8 |
| 10 Test + Harden | Full unit/integration/E2E/security/concurrency/failure-recovery; lint, format, types, migrations, Docker smoke | All relevant suites pass, no blanket suppressions | G9, G10 (concurrency part) |
| 11 Load | Locust/k6 at 1/10/25/50; measure; fix bottlenecks; report | Measured, documented, defects fixed | G10 |
| 12 Docker + Deploy | Clean local Docker verification; hosting research; deployment config | Local stack works end-to-end with persistence; hosted deploy verified or accurately documented as blocked | G11, G12 |

Gate G12 additionally requires: documentation complete, final verification report, AUDIT_REPORT.md with critical/high findings fixed.

## Interruption / resume
If context, usage (see CLAUDE.md 90% rule) or an external blocker stops work: update status files, record exact outstanding tasks and next command, commit, mark nothing incomplete as done. Resume from the last verified checkpoint.
