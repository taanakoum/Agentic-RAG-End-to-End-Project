# Implementation Status

States: NOT_STARTED | IN_PROGRESS | IMPLEMENTED | TESTED | VERIFIED | BLOCKED
VERIFIED only after verification actually passed; put evidence (command + result) in EXECUTION_LOG.md.

**Current phase:** 3 (Ingestion). Phase 2 VERIFIED (G2). Phase 1 TESTED on native services; G1 still BLOCKED on Docker (KI-003, ADR-006)
**Next step:** Phase 3: read docs/spec/03-ingestion.md and .claude/skills/rag-concurrency-reliability; start services (`scripts/dev_services.ps1 start`); branch feature/phase-3-ingestion. Needs a PDF library and OCR (Tesseract is NOT installed: KI-010). Never mark Phase 1 / G1 VERIFIED without Docker.
**Last usage reading:** could not be measured (no usage tool in session; KI-002)

## Phases / Gates
| Phase | Gate | Status | Evidence (log ref) |
|---|---|---|---|
| 0 Recon | - | VERIFIED | Log entry 1 |
| 1 Foundation | G1 | TESTED (native); G1 BLOCKED on Docker | Log entry 4; KI-003, KI-009 |
| 2 FastAPI + Auth | G2 | VERIFIED | Log entry 5 |
| 3 Ingestion | G3 | NOT_STARTED | |
| 4 Retrieval | G4 | NOT_STARTED | |
| 5 Sessions + Memory | G5 | NOT_STARTED | |
| 6 Single-agent | G6 | NOT_STARTED | |
| 7 Multi-agent | G7 | NOT_STARTED | |
| 8 Eval + Observability | - | NOT_STARTED | |
| 9 Frontend | G8 | NOT_STARTED | |
| 10 Test + Harden | G9 | NOT_STARTED | |
| 11 Load | G10 | NOT_STARTED | |
| 12 Docker + Deploy | G11, G12 | NOT_STARTED | |

## Requirements (see docs/spec/08-acceptance.md)
| Requirement | Status | Evidence |
|---|---|---|
| Interactive/background pipelines separate | NOT_STARTED | |
| Versioned modular FastAPI | TESTED (auth + health routers only) | Log entry 5 |
| Registration/login/refresh/logout/revocation | VERIFIED | Log entry 5 |
| Argon2id | VERIFIED | Log entry 5 |
| Normalized-email unique constraint | VERIFIED | Log entry 5 |
| User isolation (API/Qdrant/checkpoints/memory/jobs/storage) | PARTIAL (auth sessions only) | Log entry 5 |
| PDF validation (magic bytes + parser) | NOT_STARTED | |
| Invalid/encrypted/scanned/corrupted/oversized handling | NOT_STARTED | |
| Persistent object storage | NOT_STARTED | |
| Celery ingestion | NOT_STARTED | |
| Outbox, idempotency, retries | NOT_STARTED | |
| Qdrant indexing/retrieval | NOT_STARTED | |
| Hybrid dense+sparse+RRF | NOT_STARTED | |
| Cross-encoder rerank | NOT_STARTED | |
| Single-agent RAG | NOT_STARTED | |
| Multi-agent RAG | NOT_STARTED | |
| RAGResponse validation | NOT_STARTED | |
| Citation verification | NOT_STARTED | |
| SSE streaming | NOT_STARTED | |
| Same-thread concurrency control | NOT_STARTED | |
| Persistent sessions + PG checkpoints | NOT_STARTED | |
| Persistent memory | NOT_STARTED | |
| Rate limiting | PARTIAL (auth endpoints only; LLM/upload limits pending) | Log entry 5 |
| Centralized errors | TESTED (contract + handlers) | Log entry 5 |
| Observability (OTel + LLM tracing) | NOT_STARTED | |
| Evaluation reports | NOT_STARTED | |
| Streamlit frontend | NOT_STARTED | |
| Unit / integration / E2E / security / concurrency tests | NOT_STARTED | |
| Load test results | NOT_STARTED | |
| Docker full stack | NOT_STARTED | |
| Deployment (verified or documented blocker) | NOT_STARTED | |
| Docs + final report + audit | NOT_STARTED | |
