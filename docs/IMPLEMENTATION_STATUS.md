# Implementation Status

States: NOT_STARTED | IN_PROGRESS | IMPLEMENTED | TESTED | VERIFIED | BLOCKED
VERIFIED only after verification actually passed; put evidence (command + result) in EXECUTION_LOG.md.

**Current phase:** 12 IN_PROGRESS; hosting preparation locally tested and ready for owner review (2026-11-14, log entry 19). Phase 11/G10 load testing is explicitly SKIPPED by the owner. The owner chose to keep Streamlit and all features after reviewing Vercel/Render free-tier blockers: Community Cloud frontend alternative, Supabase private PostgreSQL schema/S3, and a paid Render reference. No staging, commit, purchase or deployment was performed; everything awaits code review. G11/G12 remain unpassed because Docker and live cloud verification are outstanding. Historical phase evidence below is retained.
**Next step:** Owner review of the uncommitted changes, `docs/VERIFICATION_REPORT.md`, `docs/AUDIT_REPORT.md` and `docs/HOSTING.md`. Full native suite: 1202 passed, 1 skipped; final private-schema regression: 33 passed; chat cleanup regression: 27 passed. Owner review comes before any commit. After separately authorized publishing/deployment, run the Docker gate and hosted verification checklist; do not resume Phase 11 unless the owner asks. Outstanding user actions remain in `docs/KNOWN_ISSUES.md` section 1.
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
| 6 Single-agent | G6 | VERIFIED (scripted-model suite + live Gemini free tier) | Log entries 11, 12; ADR-026/027 |
| 7 Multi-agent | G7 | REMOVED (user decision 2026-11-11; single agent only) | ADR-028 |
| 8 Eval + Observability | - | VERIFIED (native; OTLP verified against a local receiver only, KI-037) | Log entry 13; ADR-030; docs/EVALUATION.md, docs/OBSERVABILITY.md |
| 9 Frontend | G8 | VERIFIED (AppTest + live backend + real headless browser) | Log entries 14, 16; ADR-031, ADR-033; docs/FRONTEND.md |
| 10 Test + Harden | G9 | VERIFIED (native; Docker smoke test BLOCKED, KI-014) | Log entry 15; ADR-032; docs/TESTING_STRATEGY.md |
| 11 Load | G10 | NOT_STARTED (SKIPPED by owner 2026-11-14) | Log entry 19; no load/capacity claim |
| 12 Docker + Deploy | G11, G12 | IN_PROGRESS (hosting preparation; Docker/cloud verification pending) | Log entry 19; HOSTING.md, AUDIT_REPORT.md, VERIFICATION_REPORT.md |

## Requirements (see docs/spec/08-acceptance.md)
| Requirement | Status | Evidence |
|---|---|---|
| Interactive/background pipelines separate | VERIFIED (native) | Log entries 7, 11, 15, 19 |
| Versioned modular FastAPI | VERIFIED (native) | Log entries 5..15, 19 |
| Registration/login/refresh/logout/revocation | VERIFIED | Log entry 5 |
| Argon2id | VERIFIED | Log entry 5 |
| Normalized-email unique constraint | VERIFIED | Log entry 5 |
| User isolation (API/Qdrant/checkpoints/memory/jobs/storage) | VERIFIED (native) | Log entries 5, 7, 9..11, 15, 19 |
| PDF validation (magic bytes + parser) | VERIFIED | Log entry 7 |
| Invalid/encrypted/scanned/corrupted/oversized handling | VERIFIED | Log entry 7 |
| Persistent object storage | VERIFIED (local volume + S3 protocol via emulator) | Log entries 6, 7 |
| Celery ingestion | VERIFIED | Log entry 7 |
| Outbox, idempotency, retries | VERIFIED | Log entry 7 |
| Multiple formats (PDF, DOCX, TXT, MD, CSV) + sandboxed parsing + Docling | VERIFIED (native Windows; Linux pending) | Log entries 8, 15, 19 |
| Account deletion, breached-password check, verified-email gating | VERIFIED (native) | Log entries 8, 15, 19 |
| Orphan sweep | VERIFIED (native) | Log entries 8, 15, 19 |
| Qdrant indexing/retrieval | VERIFIED (indexing + authorized hybrid search) | Log entries 7, 9 |
| Hybrid dense+sparse+RRF | VERIFIED (tests + evaluation) | Log entry 9 |
| Cross-encoder rerank | VERIFIED (tests + evaluation; CPU cost KI-023) | Log entry 9 |
| Single-agent RAG | VERIFIED (real free model: 0 errors, 100% correct refusals, 94% gold citations) | Log entries 11, 12 |
| Multi-agent RAG | REMOVED (user decision) | ADR-028 |
| RAGResponse validation | VERIFIED (contracts, bounded repair, safe fallback) | Log entry 11 |
| Citation verification | VERIFIED (server-side, attack tests, mutation-checked) | Log entry 11 |
| SSE streaming | VERIFIED (order, replay, errors, disconnect on a real socket, keep-alive) | Log entry 11 |
| Same-thread concurrency control | VERIFIED (ChatTurnService + /chat endpoints) | Log entries 10, 11 |
| Persistent sessions + PG checkpoints | VERIFIED (real LLM-free graph; agent graphs in Phase 6) | Log entry 10 |
| Persistent memory | VERIFIED (rule-based consolidation; LLM extractor KI-026) | Log entry 10 |
| Rate limiting | VERIFIED (native; auth/upload/search/chat/model quota) | Log entries 5, 7, 9, 11, 15, 19 |
| Centralized errors | TESTED (contract + handlers) | Log entry 5 |
| Observability (OTel + LLM tracing) | VERIFIED (native) | Log entries 13, 18, 19 |
| Evaluation reports | VERIFIED (documented datasets; not a general-quality promise) | Log entries 8, 9, 11..13, 18 |
| Streamlit frontend | VERIFIED (native; cloud pending) | Log entries 14..18, 19 |
| Unit / integration / E2E / security / concurrency tests | VERIFIED (native) | Log entries 15, 18, 19; full suite 1202 passed / 1 skipped |
| Load test results | SKIPPED by owner 2026-11-14 | Log entry 19; no capacity claims |
| Docker full stack | NOT_STARTED | |
| Deployment (verified or documented blocker) | Prepared; Docker/live cloud checks pending, free-tier blockers documented | Log entry 19; HOSTING.md |
| Docs + final report + audit | IMPLEMENTED; owner review and cloud verification outstanding | Log entry 19; AUDIT_REPORT.md, VERIFICATION_REPORT.md |
