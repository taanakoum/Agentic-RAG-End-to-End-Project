# Implementation Status

States: NOT_STARTED | IN_PROGRESS | IMPLEMENTED | TESTED | VERIFIED | BLOCKED
VERIFIED only after verification actually passed; put evidence (command + result) in EXECUTION_LOG.md.

**Current phase:** 4 (Retrieval). Phases 2 and 3 VERIFIED (native). Phase 1 TESTED natively; G1 BLOCKED on Docker (KI-014).
**Next step:** (a) User runs the Docker steps in KI-014, then `powershell -File scripts/verify_g1.ps1` to verify G1. (b) Phase 4: read docs/spec/04-retrieval.md; build dense+sparse query, RRF, cross-encoder rerank, authz filters, evaluation dataset and metrics (note KI-018). Start services first (`scripts/dev_services.ps1 start`).
**Last usage reading:** could not be measured (no usage tool in session; KI-002)

## Phases / Gates
| Phase | Gate | Status | Evidence (log ref) |
|---|---|---|---|
| 0 Recon | - | VERIFIED | Log entry 1 |
| 1 Foundation | G1 | TESTED (native); G1 BLOCKED on Docker | Log entry 4; KI-003, KI-009 |
| 2 FastAPI + Auth | G2 | VERIFIED | Log entry 5 |
| 3 Ingestion | G3 | VERIFIED (native; container-restart scenario pending Docker, KI-019) | Log entry 7 |
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
| User isolation (API/Qdrant/checkpoints/memory/jobs/storage) | PARTIAL (auth sessions, documents, jobs, storage, vector maintenance filters proven; chat/checkpoints/memory/query filters pending) | Log entries 5, 7 |
| PDF validation (magic bytes + parser) | VERIFIED | Log entry 7 |
| Invalid/encrypted/scanned/corrupted/oversized handling | VERIFIED | Log entry 7 |
| Persistent object storage | VERIFIED (local volume + S3 protocol via emulator) | Log entries 6, 7 |
| Celery ingestion | VERIFIED | Log entry 7 |
| Outbox, idempotency, retries | VERIFIED | Log entry 7 |
| Qdrant indexing/retrieval | PARTIAL (indexing verified; retrieval is Phase 4) | Log entry 7 |
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
