# Architecture (Phase 0 baseline)

Authoritative requirements: `docs/spec/01-architecture.md`. This file is the working summary; it will be expanded with detailed sequence diagrams as each pipeline is implemented.

## System diagram
```
Streamlit ──HTTP/SSE──> FastAPI (/api/v1) ──> services ──> repositories ──> PostgreSQL (source of truth)
                           │                      │
                           │                      ├──> LangGraph (single / multi agent) ──> Qdrant (dense+sparse, RRF) ──> reranker ──> LLM
                           │                      │         └── Postgres checkpointer
                           │                      └──> object storage (local volume / S3-compatible)
                           └── upload ──> PG (document + job + outbox, one transaction)
                                              │ dispatcher
                                              v
                                        Redis ──> Celery workers: parse/OCR → chunk → embed → Qdrant → PG status
```

## Interactive vs background
- Interactive chat: async FastAPI + async LangGraph + SSE. Never goes through Celery.
- Background: upload → outbox → dispatcher → Redis → Celery. Idempotent tasks; persisted document state machine PENDING→VALIDATING→QUEUED→PROCESSING→EXTRACTING→CHUNKING→EMBEDDING→INDEXING→COMPLETED, any→FAILED.

## Consistency
PostgreSQL is the source of truth. Redis holds only broker/cache/rate-limit state. Qdrant is derived and rebuildable from PG + object storage. Reconciliation job to be specified in Phase 3.

## Status
No code exists yet. Sequence diagrams for ingestion and query are due in Phases 3 and 6.
