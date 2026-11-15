# Implementation Plan and Phase 0 Assessment

Current owner override (2026-11-14): Phase 11 is skipped; keep Streamlit and all features while
preparing alternative hosting and documenting free-tier blockers. Changes remain uncommitted for
review. See IMPLEMENTATION_STATUS.md and HOSTING.md for current work; the assessment below is historical.

## Phase 0 assessment (2026-11-08)

### Repository state
- Project folder held only control files (CLAUDE.md, docs/spec/*, .claude/*, status/log files). No application code, no git repo.
- `git init` run on 2026-11-08, branch `feature/phase-0-recon`. Nothing was discarded.

### Environment (measured)
| Item | Result |
|---|---|
| OS | Windows 11 Home 10.0.26200, 15.8 GB RAM, ~52 GB free on C: |
| Python | 3.14.3 default; uv-managed 3.13.12 present |
| uv | 0.10.2 |
| git | 2.53.0 |
| Docker / Podman | NOT installed |
| WSL | NOT installed (`wsl.exe --install` required) |
| Native psql / postgres / redis-server / qdrant | none installed |
| Tesseract / poppler | none installed |
| Node | not installed (not needed) |
| winget | 1.29.380 present |
| Network | PyPI reachable (HTTP 200) |

### Current stable versions on PyPI (checked 2026-11-08; pin exact versions with a lockfile in Phase 1)
fastapi 0.162.2, uvicorn 0.54.0, langgraph 1.2.20, langgraph-checkpoint-postgres 3.2.0, langchain 1.4.9, langchain-core 1.7.6, sqlalchemy 2.1.8 (>=3.11), asyncpg 0.32.0, psycopg 3.3.8, alembic 1.20.1, pydantic 2.14.3, pydantic-settings 2.16.2, celery 5.7.0, redis 8.1.0, qdrant-client 1.20.0, fastembed 0.9.1, argon2-cffi 25.1.0, pyjwt 2.15.1, streamlit 1.67.0, pypdf 7.0.0, pymupdf 2.1, pytest 9.2.0, pytest-asyncio 1.4.1.post1, ruff 0.17.4, pyright 1.1.414, locust 2.47.0, sentence-transformers 6.2.0, torch 2.15.0, ragas 0.4.3, opentelemetry-sdk 1.45.2, langfuse 5.2.0, structlog 26.1.0, testcontainers 4.15.0.

Note: these are version numbers read from the PyPI JSON API. API-level documentation for each library (FastAPI lifespan, LangGraph `AsyncPostgresSaver`, SQLAlchemy 2.1 async, Celery 5.7, Qdrant sparse/hybrid query API) must still be read from the official docs at the start of the phase that uses it. That has not been done yet.

### Target structure
As in `docs/spec/01-architecture.md` ("Target repo layout"), rooted in this folder (no extra `agentic-rag-platform/` level). Backend in `backend/app`, frontend in `frontend/streamlit_app`, etc.

### Phase plan
Phases 1..12 follow `docs/spec/09-phases-and-gates.md`. Each phase: read its spec file, implement, test, verify gate with commands, log evidence, commit.

### Hard blocker for Phase 1 / G1 (see KNOWN_ISSUES KI-003)
G1 requires building images and running PostgreSQL, Redis and Qdrant. CLAUDE.md also requires real PG/Redis/Qdrant/Celery in integration tests. None of these can run on this machine now. Options (needs the user, because each changes the machine or needs a reboot):
1. Install Docker Desktop (needs admin, WSL2/virtualization, reboot). Recommended; it is also what G1/G11/G12 require.
2. Install native Windows binaries (PostgreSQL, Redis-compatible such as Memurai, Qdrant) in user space for dev/integration only. Docker is still required for G1/G11/G12.
3. Develop against a remote machine that has Docker.

## Risk register
| ID | Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| R1 | No Docker/WSL: G1, G11, G12 and real-service integration tests cannot run | Certain (now) | High | User installs Docker Desktop; see KI-003 |
| R2 | Python 3.14 default has thinner wheel support (torch, pymupdf, etc.) | Medium | Medium | Pin Python 3.13 via uv (`.python-version`) |
| R3 | Local 15.8 GB RAM: Postgres + Qdrant + Redis + workers + reranker model + Docker VM | Medium | Medium | Small embedding/rerank models, cap Celery concurrency, limit Docker VM memory |
| R4 | No paid LLM key assumed; tests must not need paid calls | Certain | Medium | Provider abstraction + test-only fake adapters; real generation quality checks need a key supplied by the user |
| R5 | Scanned-PDF OCR needs Tesseract (not installed) | Medium | Medium | Install Tesseract inside the worker Docker image; native tests need it too |
| R6 | OneDrive-synced project folder: file locking, `.venv`/`node_modules` sync churn, path-with-space issues | Medium | Low | Keep `.venv` outside OneDrive (`UV_PROJECT_ENVIRONMENT`) or exclude it from sync |
| R7 | Weekly usage cannot be read inside a session | Certain | Medium | Checkpoint (commit + log) after every sub-milestone (KI-002) |
| R8 | Library APIs changed since training data (FastAPI, LangGraph 1.x, SQLAlchemy 2.1, Celery 5.7) | High | Medium | Read official docs before each phase; record in ADRs |
| R9 | Hosted free-tier deploy requires external accounts | Certain | Low | Document exact steps; do not create accounts without authorization (G12) |

## Phase 6 plan (updated 2026-11-11): free hosted model API, Redis caching
User decisions: no local model download; the LLM is a FREE hosted API (Google Gemini, OpenRouter `:free` models, Hugging Face Inference Providers, or Groq). All of them speak the OpenAI-compatible chat protocol, so ONE provider (`OpenAICompatibleModel`: base URL, key, model, ordered fallback models) covers them; switching provider is configuration only (`LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY`). Tests never call a hosted model (scripted deterministic model); the real end-to-end verification needs a free API key from the user (nothing is created for them, see docs/DEPLOYMENT.md). Free tiers rate-limit hard, so the provider layer has timeouts, bounded retries with backoff, a fallback-model chain, a per-user and a global concurrency limit, and stable errors (`MODEL_PROVIDER_UNAVAILABLE`, `RATE_LIMITED`).
Redis caching of frequent prompts/responses (also protects the free quota):
1. LLM-call cache: key = SHA-256 of (provider, model, schema version, temperature, the full message list); value = the raw model output that PASSED validation; TTL (default 1 h); size-bounded values; a hit skips the hosted call. Keys are namespaced per user whenever the prompt contains user data (documents, history, memory); only context-free prompts (e.g. greetings) share a global namespace. Citations are always re-verified after a cache hit, so a cached answer can never smuggle in unauthorized chunks.
2. Invalidation by construction: the prompt contains the retrieved chunks, so a changed or deleted document changes the key; nothing stale is served. No cache writes for failed, invalid, degraded or insufficient-evidence outputs.
3. Fail-open: Redis down means a cache miss, never a failed chat. Hit/miss/bypass counters are exported in the response metadata and logs. `Cache-Control`-style bypass: `agent_config` may not disable it, an admin setting `LLM_CACHE_ENABLED` does.
4. Optional later: cache the retrieval+rerank result per (user, query, knowledge-base fingerprint) to save the ~2 s reranker call.
Escalation policy for the engineer (CLAUDE.md "Model escalation"): Sonnet 5.5 executes; Opus 5.5 plans when a task is too complex or Sonnet is stuck.
