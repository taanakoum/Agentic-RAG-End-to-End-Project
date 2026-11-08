# 08 Definition of Done

Not done because it launches. Each row needs verification evidence in EXECUTION_LOG.md.

| Area | Criteria |
|---|---|
| Architecture | Interactive and background pipelines separate |
| FastAPI | Versioned, modular routers + service layer |
| Auth | Register/login/refresh/logout/revocation |
| Passwords | Argon2id |
| Duplicates | PG unique constraint on normalized email |
| Isolation | No cross-user access anywhere (API, Qdrant, checkpoints, memory, jobs, storage) |
| PDF upload | Magic bytes + parser validation |
| Documents | Invalid/encrypted/scanned/corrupted/oversized handled |
| Storage | Originals persist across restarts |
| Ingestion | Async Celery processing |
| Reliability | Idempotency, retries, durable enqueue (outbox) |
| Qdrant | Indexing + retrieval work |
| Hybrid | Dense + sparse + fusion |
| Rerank | Functional cross-encoder |
| Single-agent | Complete workflow |
| Multi-agent | Complete bounded workflow |
| Output | Pydantic-validated RAGResponse |
| Citations | Server-verified against authorized evidence |
| Streaming | Functional typed SSE |
| Concurrency | Many users; independent conversations; same-thread writes controlled |
| Sessions | Persistent conversations + checkpoints |
| Memory | Persistent, user-specific, deletable |
| PostgreSQL | Migrations + constraints |
| Redis | Broker/cache/rate limit only |
| Observability | Traces, logs, metrics |
| Evaluation | Reproducible quality + performance reports |
| Errors | Centralized, machine-readable codes |
| Frontend | Complete Streamlit app via API |
| Tests | Unit, integration, E2E, security, concurrency pass |
| Load | Measured results with documented conditions |
| Docker | Full local stack runs from clean |
| Deployment | Verified deployment or accurately documented limitation |
| Docs | Setup, ops, architecture, API, security complete |

## Final end-to-end scenario (all 30 must work)
1 register; 2 Argon2id hash; 3 duplicate normalized email rejected by PG constraint; 4 login; 5 upload PDF in Streamlit; 6 backend validates real content, rejects invalid; 7 original stored persistently; 8 job reliably enqueued; 9 Celery processes; 10 extract + chunk; 11 dense + sparse generated; 12 indexed in Qdrant; 13 document retrievable; 14 create conversation; 15 ask question; 16 single or multi workflow runs; 17 hybrid retrieval selects evidence; 18 rerank refines; 19 structured answer; 20 Pydantic validates; 21 citations verified vs authorized chunks; 22 streams to Streamlit; 23 message + graph state persisted; 24 conversation survives restart; 25 memory available in another session; 26 second user cannot access anything; 27 concurrent multi-user works; 28 same-thread conflicts handled safely; 29 invalid inputs/failures/retries predictable; 30 covered by automated tests.

## Final verification report must include
Implemented features; architecture overview; key decisions; install/run commands; test commands; ACTUAL test, evaluation and load results; deployment status/URL if verified; known bugs/limitations; security audit findings; deferred features; next steps. Partial features: say exactly what remains.

## Final audit (before declaring completion; fix critical/high findings)
Architecture (pipelines separate, async correct, CPU isolated, pools, multi-instance), Security (Argon2id, unique email, JWT, rotation, isolation, uploads, tool perms), RAG quality (fusion real, rerank correct, citations genuine, filters, chunking evaluated, insufficient evidence, embedding version consistency), Agent reliability (loops, transitions, tools, structure, benefit proven, injection, recovery), Concurrency (registration, uploads, independent convs, same-thread, idempotency, duplicate tasks), Persistence (files, convs, checkpoints, memory, deletion propagation, Redis loss, backups), Operations (traceability, monitoring, bounded retries, queue monitoring, free-deploy feasibility, load results). Write the audit report to `docs/AUDIT_REPORT.md`.
