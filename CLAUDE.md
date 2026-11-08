# Agentic RAG Platform: Project Instructions for Claude Code

You are the autonomous engineer building this project. Read this file fully at the start of every session.

## Mission
Build a production-grade, async, multi-user Agentic RAG platform (single-agent + multi-agent LangGraph modes) with FastAPI, PostgreSQL, Qdrant, Redis/Celery, Streamlit, Docker. Correctness, security, persistence and measurable RAG quality matter more than feature count.

Authoritative requirements live in `docs/spec/` (start with `docs/spec/README.md`). Do not re-derive requirements from memory; read the relevant spec file before each phase.

## Session start protocol (every session)
1. Read `docs/IMPLEMENTATION_STATUS.md`, the tail of `docs/EXECUTION_LOG.md`, and `docs/KNOWN_ISSUES.md`.
2. Run `git status`, `git branch --show-current`, `git log -5 --oneline`. Never discard uncommitted work.
3. Find the first phase not VERIFIED (see `docs/spec/09-phases-and-gates.md`) and resume at its recorded "next step".
4. Read the spec file(s) for that phase, then execute. Do not ask for approval of ordinary decisions; record them in `docs/ARCHITECTURE_DECISIONS.md`.

## USAGE LIMIT RULE (hard stop at 90% weekly usage)
- Check usage with `/usage` (or the status line) at the start of each session, before starting each phase, and after every commit/milestone.
- If weekly usage is >= 90%: STOP starting new work. Finish only the atomic step in progress if it can be left consistent, then run `/checkpoint` (see `.claude/commands/checkpoint.md`): update status/log/known-issues, record the exact next command, commit, and end the session with a short handoff message.
- If usage cannot be read, checkpoint after every sub-milestone anyway (commit + log), so a cutoff never loses state, and tell the user the limit could not be measured.
- Do not spawn subagents when usage is above ~75%. Subagents are expensive. Only use them for independent tasks with explicit file ownership.
- Never claim background work continues after the session ends.

## Non-negotiables (full detail in spec)
- Argon2id passwords. Unique constraint on normalized email in PostgreSQL (app-level checks are insufficient).
- Versioned `/api/v1/` routers; routers = HTTP only, services = logic, repositories = DB, Pydantic schemas = contracts. Never expose ORM models.
- PDF validation by magic bytes + parser + limits, never by extension/MIME alone.
- Interactive chat (async FastAPI + LangGraph + SSE) is separate from background work (Celery/Redis). Do not route chat through Celery.
- Transactional outbox for DB -> Celery enqueue. Idempotent tasks. Persisted state machine for documents.
- PostgreSQL is source of truth. Redis is never the only copy of anything critical.
- Qdrant dense + sparse, RRF fusion, cross-encoder rerank; user/tenant filter on EVERY query.
- Final answers are a versioned Pydantic `RAGResponse`; every citation verified server-side against authorized chunks retrieved in that execution.
- Persistent LangGraph checkpointer (Postgres). Same-thread concurrent writes explicitly controlled (advisory lock or equivalent). Idempotency keys on chat.
- Centralized exceptions and stable error codes; no stack traces to users; request IDs everywhere.
- Tests must not need paid LLM calls; real Postgres/Redis/Qdrant/Celery in integration/E2E.
- Pinned deps + lockfile; Ruff + Pyright clean without blanket suppressions.

## Honesty rules
- Never fabricate test results, benchmarks, load numbers, or deployment URLs. Never hide failing tests.
- Status VERIFIED only after the verification actually passed this project (record the command + result in the log).
- No placeholder implementations of required features. Mocks only in tests, via clearly separated test adapters.
- Do not create external accounts, spend money, or deploy without user authorization; if blocked, document exact steps.
- Check current official docs for FastAPI, LangGraph, LangChain, SQLAlchemy, Pydantic, Celery, Qdrant before use. Do not copy old tutorials. Network may be limited: if package installs fail, record the blocker and do what is possible.

## Working method
Small increments: define -> implement -> test -> review -> integrate -> verify -> update docs -> commit. Work on a feature branch, commit per milestone. Keep the app runnable. Do not build on unverified components. A quality gate (G1..G12) is passed only by actual verification.

## Control files you must keep current
`docs/IMPLEMENTATION_STATUS.md`, `docs/EXECUTION_LOG.md`, `docs/KNOWN_ISSUES.md`, `docs/ARCHITECTURE_DECISIONS.md` (plus ARCHITECTURE, IMPLEMENTATION_PLAN, SECURITY, DATA_MODEL, API_SPECIFICATION, TESTING_STRATEGY, DEPLOYMENT as the system takes shape).

## Skills and commands
- Skills in `.claude/skills/`: `rag-phase-runner`, `rag-verification-gate`, `rag-concurrency-reliability`, `rag-citation-and-response`, `rag-checkpoint-handoff`.
- Commands: `/next-phase`, `/checkpoint`, `/verify-gate`.

## Progress reports
After each phase: phase, components built, tests actually run + results, problems found/resolved, remaining work, next step. Keep it concise.
