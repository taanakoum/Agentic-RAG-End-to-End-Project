---
name: rag-concurrency-reliability
description: Patterns and checklists for race-safe, idempotent, multi-user code in this project (unique constraints, outbox, Celery idempotency, same-thread LangGraph locking, chat idempotency keys, deletion consistency). Use when writing or reviewing registration, uploads, tasks, chat, checkpoints, or deletion.
---

# Concurrency and reliability patterns

- **Uniqueness**: DB constraints are the authority. Insert, catch IntegrityError, map to 409. Never check-then-insert alone. Applies to email (normalized), (user_id, content_hash), idempotency keys.
- **Outbox**: write business row + job + outbox event in one transaction; dispatcher uses `SELECT ... FOR UPDATE SKIP LOCKED`, publishes, marks sent; reconciler requeues stale QUEUED/PROCESSING. Publishing is at-least-once, so tasks must be idempotent.
- **Idempotent tasks**: key by job id + document version; check state before work; deterministic Qdrant point IDs (e.g. uuid5 of doc/version/chunk) so upsert is safe; `acks_late`, bounded retries, exponential backoff + jitter, time limits; verify ownership at task start.
- **State machine**: transitions in one function with an allowed-transition table; update with compare-and-set (`WHERE status = :expected`).
- **Same-thread chat**: acquire a per-thread lock (PG advisory lock keyed on thread id hash, or row version) before graph execution; on contention return 409 CONCURRENCY_CONFLICT (or queue with a documented timeout). Release in finally, including on client disconnect/cancel. Different threads run in parallel. Session deletion during run must be defined and tested.
- **Chat idempotency**: unique (session_id, idempotency_key); replay returns the stored result or in-progress status; no duplicate execution.
- **Deletion**: tombstone in PG -> journal/outbox -> purge Qdrant vectors and storage objects idempotently -> reconcile orphans. Failure in any store must be retried, never silently ignored.
- **Event loop**: no CPU-bound or blocking calls in async endpoints (rerank, parsing, embeddings of local models): use executors/workers. Bound concurrency with semaphores. Celery/asyncio boundary per ARCHITECTURE_DECISIONS.
- **Tests**: use real concurrency (asyncio.gather / threads) against real PG, assert one winner/consistent final state.
