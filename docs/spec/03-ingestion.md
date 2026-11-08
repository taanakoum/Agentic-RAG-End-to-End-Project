# 03 PDF Ingestion, Storage, Reliability

PDF is the only mandatory format. Build the parser interface to be extensible; only expose formats that are implemented and tested. (TXT/MD/DOCX/CSV from the earlier prompt are deferred; log in KNOWN_ISSUES.)

## Upload UX (Streamlit)
File picker, name, size, upload progress where feasible, validation status, processing status, completion, failure details, retry where safe, delete. Backend is authoritative for validation.

## Validation (independent checks, backend)
1 presence 2 extension 3 declared MIME 4 magic bytes (`%PDF-`) 5 parser validity 6 readability 7 size 8 page count 9 encryption/password 10 extractable text 11 parser resource use.
Handle explicitly: no file, missing multipart field, empty, renamed non-PDF, wrong MIME, corrupted, malformed, oversized (413), too many pages, encrypted, no extractable text, parser error/timeout, duplicate, upload timeout, OCR failure. Unsupported type -> 415. Limits configurable. Secure parsing libs, resource limits, consider sandbox/malware scan.

## Scanned PDFs / OCR
Detect insufficient text; optional configurable OCR fallback in the background worker only; resource limits; record OCR status; never index empty/meaningless chunks; if extraction fails, explain to user.

## Document states (persisted, transitions enforced)
PENDING -> VALIDATING -> QUEUED -> PROCESSING -> EXTRACTING -> CHUNKING -> EMBEDDING -> INDEXING -> COMPLETED; any processing stage -> FAILED (recoverable vs permanent flagged); FAILED -> QUEUED via explicit retry/re-index. Illegal transitions raise. Not retrievable until COMPLETED.

## Parsing and chunking
Text extraction, metadata, normalization, structure-aware parsing, configurable chunk size/overlap, recursive chunking, heading/section preservation, page metadata. Choose chunk settings by **evaluation**, not guess.
Chunk payload: user_id, document_id, document_version, chunk_id, filename, page_number, heading/section, ingestion_timestamp, embedding_model_version.

## Dedup
SHA-256 of original bytes, scoped per owner. DB unique constraint (user_id, content_hash) (consider partial index for non-deleted). Handle concurrent duplicate uploads transactionally (insert-or-fetch). Avoid reparse/re-embed; re-index only explicit. Never reveal other users' documents.

## Storage abstraction
upload, download/controlled access, delete, metadata, exists, unique keys, integrity verification. Local persistent volume (dev) + S3-compatible (hosted). Keys generated, never from raw filenames; prevent traversal; ownership checks; retention + orphan cleanup. Document persistence per environment. Must survive container restarts.

## Reliable enqueue (transactional outbox)
Document row + job row + outbox row in ONE transaction. A dispatcher (Beat or loop; uses `FOR UPDATE SKIP LOCKED`) publishes outbox rows to Celery and marks them sent. Reconciler requeues jobs stuck in QUEUED/PROCESSING past TTL. Worker tasks: idempotent (keyed by job/document version), safe on duplicate delivery, `acks_late` + retries with exponential backoff, time limits, verify ownership.

## Distributed failure cases to design and test
- doc row saved, task never published -> outbox dispatcher
- task delivered twice -> idempotency
- worker crash mid-ingest -> reconciler + resume/restart idempotently
- embeddings OK but Qdrant fails -> retry, state stays INDEXING/FAILED, no COMPLETED
- Qdrant OK but DB status update fails -> idempotent upsert (deterministic point IDs), reconciler
- object upload OK but DB commit fails -> orphan cleanup
- PG delete OK but Qdrant delete fails -> deletion journal/outbox + retry
- model completes but message persist fails -> documented behavior, no silent success

## Lifecycle
Versioning, re-indexing, replacement, failed-processing recovery, deletion across PostgreSQL + Qdrant + object storage with a documented consistent strategy (soft-delete/tombstone -> async purge -> reconcile), retention controls, orphan cleanup.

## Idempotency (general)
Uploads, registrations, chat submissions, memory updates, background tasks, indexing, deletion: DB constraints, unique task IDs, transactions, idempotency keys.
