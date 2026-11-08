# Architecture Decisions

Format: ID, date, context, options considered, decision, consequences. Required topics (record when decided): LangGraph checkpoint persistence; same-thread concurrency mechanism; outbox + dispatcher design; Celery/asyncio execution model; Qdrant collection layout + sparse method + point ID scheme; PDF/object storage; memory storage; streaming validation; job recovery/reconciliation; deletion consistency; Streamlit auth session handling; free-hosting choices.

## ADR-001 (2026-11-08): Target Python 3.13, managed by uv
- Context: default interpreter is 3.14.3; uv has 3.13.12 installed. Planned deps include torch, pymupdf, sentence-transformers, which lag on newest Python versions.
- Options: 3.14, 3.13, 3.12.
- Decision (provisional): 3.13 via `.python-version` + uv lockfile. To be confirmed in Phase 1 by an actual `uv sync` of the full dependency set; if resolution fails, fall back to 3.12 and amend this ADR.
- Consequences: the Docker base image must use the same minor version.

## ADR-002 (2026-11-08): Repo root is the project folder
- Decision: spec layout is rooted directly here (`backend/`, `frontend/`, `docs/` ...) instead of a nested `agentic-rag-platform/` folder, since the folder already holds CLAUDE.md and docs.
