# Spec Index

Condensed from three source documents: (1) Master Engineering Specification, (2) Addendum sections 24-34 (pipeline separation, concurrency, PDF, routers, Pydantic, errors, auth), (3) earlier Master Prompt, plus (4) the Execution Directive. This folder is the working requirements set for Claude Code.

| File | Content |
|---|---|
| 01-architecture.md | Stack, components, pipelines, infra roles, repo layout |
| 02-auth-security.md | Auth, isolation, security, prompt injection |
| 03-ingestion.md | PDF validation, OCR, states, chunking, dedup, outbox, storage, deletion |
| 04-retrieval.md | Dense/sparse/RRF/rerank, citations |
| 05-agents-chat-memory.md | Single agent (multi-agent removed), RAGResponse, SSE, sessions, checkpoints, concurrency, memory |
| 06-api-errors-frontend.md | Endpoints, error schema, Streamlit |
| 07-testing-eval-ops.md | Tests, evaluation, observability, load, Docker, deployment |
| 08-acceptance.md | Definition of done + final end-to-end scenario |
| 09-phases-and-gates.md | Phases 0-12, exit conditions, gates G1-G12 |

## Conflict resolutions between source documents
- Formats: the master spec makes **PDF the only mandatory format** and says to expose only tested formats. The older prompt listed TXT/MD/DOCX/CSV; treat those as deferred (design the parser interface so they can be added; document in KNOWN_ISSUES).
- Document states: use the detailed lifecycle PENDING -> VALIDATING -> QUEUED -> PROCESSING -> EXTRACTING -> CHUNKING -> EMBEDDING -> INDEXING -> COMPLETED, any stage -> FAILED (with retry path). Enforce valid transitions.
- Endpoints: all under `/api/v1/`. Include `GET /documents/{id}/status` as an alias if convenient, but ingestion router status endpoints are canonical.
- Where specs differ in strictness, the stricter requirement wins.
- Interactive chat is never forced through Celery.

## How to use
Before a phase, read the matching spec file(s). Cross-check the deliverable against `08-acceptance.md` before marking VERIFIED.
