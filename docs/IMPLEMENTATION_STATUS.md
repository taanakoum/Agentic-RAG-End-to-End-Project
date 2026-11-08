# Implementation Status

States: NOT_STARTED | IN_PROGRESS | IMPLEMENTED | TESTED | VERIFIED | BLOCKED
VERIFIED only after verification actually passed; put evidence (command + result) in EXECUTION_LOG.md.

**Current phase:** 6 (Single-agent). Phases 2, 3, 4 and 5 VERIFIED (native). Phase 1 TESTED natively; G1 BLOCKED on Docker (KI-014).
**Next step:** (a) User installs WSL2 and runs the Docker steps in KI-014 / docs/DEPLOYMENT.md, then `powershell -File scripts/verify_g1.ps1` to verify G1. (b) Phase 6: read docs/spec/05-agents-chat-memory.md (single-agent graph, RAGResponse, citation verification, SSE) and the rag-citation-and-response skill; use the ChatTurnService, RetrievalService, MemoryService.relevant and the Postgres checkpointer built in Phases 4-5. A model provider is needed (no paid calls in tests).
**Last usage reading:** could not be measured (no usage tool in session; KI-002)

## Phases / Gates
| Phase | Gate | Status | Evidence (log ref) |
|---|---|---|---|
| 0 Recon | - | VERIFIED | Log entry 1 |
| 1 Foundation | G1 | TESTED (native); G1 BLOCKED on Docker | Log entry 4; KI-003, KI-009 |
| 2 FastAPI + Auth | G2 | VERIFIED | Log entry 5 |
| 3 Ingestion | G3 | VERIFIED (native; container-restart scenario pending Docker, KI-019) | Log entry 7 |
| 4 Retrieval | G4 | VERIFIED (native) | Log entry 9; ADR-022 |
| 5 Sessions + Memory | G5 | VERIFIED (native) | Log entry 10; ADR-023..025 |
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
| Multiple formats (PDF, DOCX, TXT, MD, CSV) + sandboxed parsing + Docling | TESTED (Windows) | Log entry 8 |
| Account deletion, breached-password check, verified-email gating | TESTED | Log entry 8 |
| Orphan sweep | TESTED | Log entry 8 |
| Qdrant indexing/retrieval | VERIFIED (indexing + authorized hybrid search) | Log entries 7, 9 |
| Hybrid dense+sparse+RRF | VERIFIED (tests + evaluation) | Log entry 9 |
| Cross-encoder rerank | VERIFIED (tests + evaluation; CPU cost KI-023) | Log entry 9 |
| Single-agent RAG | NOT_STARTED | |
| Multi-agent RAG | NOT_STARTED | |
| RAGResponse validation | NOT_STARTED | |
| Citation verification | NOT_STARTED | |
| SSE streaming | NOT_STARTED | |
| Same-thread concurrency control | VERIFIED (ChatTurnService; HTTP chat endpoints in Phase 6) | Log entry 10 |
| Persistent sessions + PG checkpoints | VERIFIED (real LLM-free graph; agent graphs in Phase 6) | Log entry 10 |
| Persistent memory | VERIFIED (rule-based consolidation; LLM extractor KI-026) | Log entry 10 |
| Rate limiting | PARTIAL (auth endpoints only; LLM/upload limits pending) | Log entry 5 |
| Centralized errors | TESTED (contract + handlers) | Log entry 5 |
| Observability (OTel + LLM tracing) | NOT_STARTED | |
| Evaluation reports | PARTIAL (chunking + retrieval reports; generation/agent evals pending) | Log entries 8, 9 |
| Streamlit frontend | NOT_STARTED | |
| Unit / integration / E2E / security / concurrency tests | NOT_STARTED | |
| Load test results | NOT_STARTED | |
| Docker full stack | NOT_STARTED | |
| Deployment (verified or documented blocker) | NOT_STARTED | |
| Docs + final report + audit | NOT_STARTED | |
