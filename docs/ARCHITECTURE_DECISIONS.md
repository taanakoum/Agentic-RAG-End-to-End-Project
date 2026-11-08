# Architecture Decisions

Format: ID, date, context, options considered, decision, consequences. Required topics (record when decided): LangGraph checkpoint persistence; same-thread concurrency mechanism; outbox + dispatcher design; Celery/asyncio execution model; Qdrant collection layout + sparse method + point ID scheme; PDF/object storage; memory storage; streaming validation; job recovery/reconciliation; deletion consistency; Streamlit auth session handling; free-hosting choices.

## ADR-001 (2026-11-08): Target Python 3.13, managed by uv
- Context: default interpreter is 3.14.3; uv has 3.13.12 installed. Planned deps include torch, pymupdf, sentence-transformers, which lag on newest Python versions.
- Options: 3.14, 3.13, 3.12.
- Decision (provisional): 3.13 via `.python-version` + uv lockfile. To be confirmed in Phase 1 by an actual `uv sync` of the full dependency set; if resolution fails, fall back to 3.12 and amend this ADR.
- Consequences: the Docker base image must use the same minor version.

## ADR-002 (2026-11-08): Repo root is the project folder
- Decision: spec layout is rooted directly here (`backend/`, `frontend/`, `docs/` ...) instead of a nested `agentic-rag-platform/` folder, since the folder already holds CLAUDE.md and docs.

## ADR-001 update (2026-11-08)
`uv sync` of the Phase 1 dependency set resolved and all tests ran on Python 3.13.12. Heavy ML dependencies (torch, sentence-transformers, langgraph) are not yet in the lock; revisit if resolution fails when they are added.

## ADR-003 (2026-11-08): Celery/asyncio execution model
- Context: spec requires one explicit model, no ad-hoc `asyncio.run()`.
- Decision: Celery tasks are plain synchronous functions using synchronous clients (SQLAlchemy sync engine over psycopg, redis-py, qdrant-client). The API uses the async stack (asyncpg, redis.asyncio, AsyncQdrantClient). Alembic uses the sync psycopg engine.
- Consequences: two DB drivers (asyncpg for the API, psycopg for workers/migrations; psycopg is also needed later for the LangGraph Postgres checkpointer). Celery config: acks_late, reject_on_worker_lost, prefetch 1, visibility_timeout = hard time limit + 300 s. On Windows dev, workers must use `--pool=solo` (prefork is unsupported); Docker/Linux uses prefork.

## ADR-004 (2026-11-08): Object storage keys and adapter
- Decision: synchronous `ObjectStorage` protocol. Keys are application-generated '/'-separated segments matching `[A-Za-z0-9_][A-Za-z0-9._-]{0,127}`; never derived from user filenames. Local adapter writes via temp file + fsync + `os.replace` (atomic), computes SHA-256 and size while streaming, enforces `max_bytes`, and verifies the resolved path stays inside the root. The S3-compatible adapter is deferred (KI-007).

## ADR-005 (2026-11-08): Health endpoints
- `/health/live` (no dependencies), `/health/ready` (PostgreSQL, Redis, Qdrant, storage; 503 on failure), `/health/workers` (Celery control ping; 503 if none). Unauthenticated; responses carry only ok/error per component, details go to logs. Worker ping uses a fresh broker connection per probe because a pooled connection went stale after a Redis restart (found in manual failure testing, see EXECUTION_LOG Entry 4).

## ADR-006 (2026-11-08): Proceed to Phase 2 while G1 is blocked
- Context: G1 needs Docker (KI-003). Phase 1 backend components are verified on native services; only the Docker artifacts are unverified.
- Decision: continue with Phase 2 (its gate G2 needs only PostgreSQL/Redis, available natively). Phase 1 stays "TESTED (native), G1 BLOCKED" until Docker verifies it. Docker-dependent work is never marked VERIFIED.

## ADR-007 (2026-11-08): Access/refresh token design
- Decision: short-lived HS256 JWT access tokens (15 min) bound to a session id (`sid` = refresh-token family) + opaque rotating refresh tokens stored hashed in PostgreSQL. Each authenticated request verifies in PostgreSQL that the user is active and the session has a live refresh token.
- Options rejected: pure stateless JWT (no immediate logout/revocation); Redis-only session store (Redis must never be the only copy of critical data).
- Consequences: one small indexed query per authenticated request in exchange for immediate revocation; reuse of a rotated refresh token revokes the family; concurrent refresh of one token yields exactly one success. HS256 with a single secret: no key rotation yet (documented in SECURITY.md).

## ADR-008 (2026-11-08): Rate limiting fails closed
- Decision: Redis fixed-window counters; on Redis failure auth endpoints return 503 rather than skipping limits. Per-account lockout counts failures only and is keyed on the submitted identity (existing or not) to avoid account enumeration.
- Consequences: a Redis outage disables login/registration (security over availability). Fixed-window boundary bursts and deliberate victim lockouts are accepted limitations (SECURITY.md).

## ADR-009 (2026-11-08): Error handling and middleware
- Decision: one `AppError` hierarchy rendered by central handlers into a single JSON contract; validation errors omit submitted values; pure ASGI middlewares for request IDs and body-size limits (no BaseHTTPMiddleware, so SSE streaming stays safe). The body-size middleware answers 413 itself and signals disconnect to the app, because FastAPI converts exceptions raised from `receive()` into 400.
