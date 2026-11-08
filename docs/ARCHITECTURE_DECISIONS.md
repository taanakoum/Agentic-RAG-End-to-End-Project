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

## ADR-010 (2026-11-08): One-time tokens, email delivery, MFA, key rotation
- One-time tokens (password reset 1 h, email verification 24 h): 384-bit random, only SHA-256 stored in `one_time_tokens`, consumed with a single atomic `UPDATE ... WHERE used_at IS NULL AND expires_at > now RETURNING` (concurrent use of one token succeeds exactly once). A new request invalidates older unused tokens of the same purpose. A weak new password rolls back the consume so the token stays usable.
- `forgot` always returns 202 and does the lookup + email in a background task; rate limits (per IP and per submitted email, existing or not) are applied in-request so the response never depends on the account.
- Password reset signs out every session; password change signs out every other session. Both send a best-effort notification email.
- Email goes through an `EmailSender` protocol: `file` backend (writes .eml; dev/test only, prod config forbids it) and `smtp` (stdlib smtplib in a thread, STARTTLS/SSL). The SMTP adapter is tested against a real aiosmtpd server.
- MFA: RFC 6238 TOTP via pyotp (30 s step, +/-1 step window), secret stored Fernet-encrypted (`MFA_ENCRYPTION_KEY`), replay protection by atomically advancing `last_used_step`, 10 single-use recovery codes stored as SHA-256, attempts rate limited per user (5 / 5 min). Login returns `mfa_required` + a 5-minute `mfa` JWT (different `type`, no `sid`) that only `/auth/mfa/verify` accepts.
- JWT key rotation: tokens carry `kid`; `JWT_KEY_ID`/`JWT_SECRET` sign, `JWT_RETIRED_KEYS` (JSON kid->secret) only verify. Unknown kid is rejected; a token without kid verifies only against the active key. Procedure: set new active key, move the old one to retired, remove it after `ACCESS_TOKEN_TTL_S`.

## ADR-011 (2026-11-08): S3 adapter
- boto3 (sync, matching the sync `ObjectStorage` protocol), `upload_fileobj` for streaming/multipart with a counting+hashing reader (enforces `max_bytes`, aborts multipart on failure), `open()` returns a seekable spooled temp copy (PDF parsers need seek), `delete_prefix` paginates + batch deletes, optional key prefix to share a bucket between environments. Selected with `STORAGE_BACKEND=s3`. HEAD cannot distinguish a missing bucket from a missing key, so `exists()` returns False there; reads/writes/deletes and the readiness probe fail loudly.

## ADR-012 (2026-11-08): Qdrant layout, point ids, payload
- One collection `<prefix>chunks`; named vectors `dense` (cosine, size `EMBEDDING_DIM`, default 384) and `sparse` (IDF-modified BM25 term weights from fastembed `Qdrant/bm25`). Keyword payload indexes on `user_id` (marked tenant index) and `document_id`.
- Point id = `uuid5(NAMESPACE, "<document_id>:<version>:<chunk_index>")`: re-running a job overwrites the same points, so duplicate delivery, retries and "Qdrant OK but DB update failed" cannot create duplicates. After a successful index of version N the worker deletes the document's points of other versions.
- Payload: user_id, document_id, document_version, chunk_id (= point id), chunk_index, filename, page_number, heading, ingestion_timestamp, embedding_model_version, text. Chunk text lives in the payload (Qdrant is a derived index, rebuildable from object storage + PostgreSQL by re-indexing) so citation excerpts can be verified against stored text later.
- Every maintenance filter (delete/count) includes BOTH user_id and document_id. Query-time filters arrive in Phase 4.
- Embedder is a small protocol (`embed_dense`, `embed_sparse`). Production: FastEmbed (BAAI/bge-small-en-v1.5 as the quantized ONNX build, plus bm25), run locally. Tests use a deterministic hash embedder defined only under `backend/tests/`; two tests use the real models.

## ADR-013 (2026-11-08): OCR for scanned PDFs: RapidOCR (open source), not Tesseract
- Requirement: OCR fallback in the background worker only, bounded, status recorded. Tesseract is not installed here and needs a system install.
- Chosen: pypdfium2 (render page, BSD/Apache) + RapidOCR (PP-OCR ONNX models bundled inside the wheel, Apache-2.0) on onnxruntime CPU. No download, no system package, same behaviour natively and in Docker. Measured on the dev laptop: model load 2.7 s, then ~1.1 s per page at 200 DPI; the generated test page ("INVOICE NUMBER 48213 / TOTAL AMOUNT DUE 1250 DOLLARS / ...") was read back exactly.
- Docling was considered (open source, layout + table aware, uses RapidOCR internally on some platforms) and NOT adopted for now: it pulls PyTorch and layout models for features (table structure, reading order) that the spec does not require yet, and it would be a much heavier worker image. The parser/OCR code sits behind small interfaces (`OcrEngine`, `extract_*`), so Docling can be added later if evaluation shows chunk quality gains.
- Limits: `OCR_ENABLED`, `OCR_MAX_PAGES` (40), `OCR_DPI` (200), `MIN_TEXT_CHARS_PER_PAGE` (25 alphanumerics below which a page is OCRed). Result recorded on the document: `ocr_status` (not_needed/used/limited/failed/disabled) and `ocr_pages`. Pages that stay empty are never chunked; a document with no meaningful text fails permanently with `DOCUMENT_NO_TEXT` and an explanation.
- Known limit: the bundled PP-OCR models target English/Chinese text; other scripts need other recognition models (not configured).

## ADR-014 (2026-11-08): Outbox, reconciliation, retries, deletion
- Upload commits document + ingestion_job + outbox_event in ONE transaction (insert-or-fetch on the partial unique index `(user_id, content_hash) WHERE deleted_at IS NULL` decides concurrent identical uploads; the loser deletes its own stored copy).
- Dispatcher: `SELECT ... FOR UPDATE SKIP LOCKED`, `send_task`, mark sent (at-least-once). Runs (a) right after upload in the API (best effort, low latency), (b) every `OUTBOX_DISPATCH_INTERVAL_S` from Celery Beat. A broker outage leaves rows unsent with an error recorded.
- Tasks: job claimed by compare-and-set `queued -> processing` (duplicate deliveries skip); every document stage change is a compare-and-set on status; `acks_late` + `reject_on_worker_lost`; transient failures release job/document to queued and retry with exponential backoff + jitter up to `INGESTION_MAX_ATTEMPTS`, then FAILED (recoverable, retry via API); permanent failures (corrupt, encrypted, no text, integrity, ownership) FAIL immediately (not recoverable). Workers verify document ownership and the stored file's SHA-256 before processing.
- Reconciler (Beat, every `RECONCILE_INTERVAL_S`): republish queued jobs whose message vanished (at most once per TTL), recover jobs stuck in processing (worker died) or fail them after max attempts, republish unfinished purges, prune sent outbox rows older than 7 days.
- Deletion: `DELETE` tombstones the row (`deleted_at`, instantly invisible to the API) and writes a purge outbox event in the same transaction; the purge task removes Qdrant points (user_id + document_id filter), the storage prefix, then hard-deletes the row LAST, so any failure is retried by the task and, if lost, by the reconciler. A purge waits while a job for the document is processing; a worker that finishes after a delete removes the vectors it wrote.
- State table deviation: PENDING and VALIDATING exist in the state machine, but because validation is synchronous in the API a new document is inserted directly as QUEUED.

## ADR-015 (2026-11-08): Docling is the default PDF pipeline; OCR only when a page needs it (supersedes ADR-013's "Docling not adopted")
- Pipelines (`PDF_PIPELINE`): `docling` (default), `light` (pypdf + RapidOCR, cheap fallback), `structured` (TXT/MD/CSV/DOCX).
- Docling converts with layout and table understanding; OCR is switched on only if at least one page has no usable text layer. Text PDFs and all non-PDF formats never touch OCR.
- Chunking uses Docling's `HybridChunker` (320 tokens, heading context prepended for embedding). Pages Docling recognised but did not chunk (e.g. a scanned page it labelled a heading) fall back to the light chunker, so no page is silently dropped.
- Evidence: `evaluation/reports/chunking_eval.md` (6 strategies, 36 questions, real embedding models). Docling HybridChunker: hybrid hit@3 0.81, MRR 0.70, versus fixed 1000-char chunks 0.56 / 0.55. Docling markdown + LangChain header splitter scored slightly higher on this corpus (hit@3 0.86, MRR 0.72) but depends on a markdown export; the HybridChunker was kept because it is token-aware, keeps tables intact and keeps page numbers per chunk, which citations need. The corpus is synthetic and small (KI-018).
- LangChain splitters were evaluated, not adopted as a pipeline dependency. Cost: about 1.7 s/page CPU for conversion (KI-021).

## ADR-016 (2026-11-08): Sandboxed parsing with warm process pools
- Untrusted files are parsed only in child processes: wall-clock timeout, memory cap (Windows Job Object; POSIX `RLIMIT_DATA`), no process spawning (Windows), scrubbed environment (no secrets), JSON-lines protocol.
- The child runs the base interpreter with the venv's site-packages on `PYTHONPATH` (the venv launcher is itself a process and is blocked by the job's active-process limit).
- `PersistentSandbox` keeps a warm child (models loaded once), recycles it after `max_jobs`, and kills it on timeout or crash; `SandboxPool` serves concurrent callers. Inspection (cheap) and parsing (heavy) use separate pools; one-shot mode exists for tests.
- Trade-off: a warm child is reused across documents of different users, so it is recycled periodically and keeps no per-user state; one child per document would be stricter but slower.

## ADR-017 (2026-11-08): Chunking method
- Sentence- and section-aware packing with a token budget; headings detected per line and tracked as a section path; chunks may span page breaks and record `page_number`..`page_end`; headers/footers removed, hyphenation repaired, hard-wrapped lines unwrapped before chunking.
- Embedding text = `title > section path` + body (contextual header); the stored and cited text is the unmodified body.
- CSV is chunked as row groups that repeat the header row; Markdown by heading; DOCX by heading styles and tables.

## ADR-018 (2026-11-08): Multiple formats, validated by content
- `FORMATS` maps extension to (format, allowed MIME types, stored MIME); the declared extension/MIME is only a hint. PDF: magic bytes + parser. DOCX: zip structure, entry count/size/ratio limits, XML parsed without entities. TXT/MD/CSV: size limit, binary rejection, encoding detection, row/column caps. Same state machine, outbox and indexing as PDFs; `documents.source_format` and `documents.parser` are recorded (migrations 0005, 0007).

## ADR-019 (2026-11-08): Account deletion, breached passwords, verified-email gating
- Deletion: `DELETE /auth/account` with password (and MFA code when enabled). One transaction revokes sessions, deactivates and tombstones the user, tombstones every document with purge events and queues a `purge_user` event. The worker purges vectors, files and finally the user row (email freed). Idempotent; the reconciler requeues unfinished purges.
- Breached passwords: offline check against about 135k SecLists passwords (MIT) plus a "core" reduction (trailing digits/symbols, leet). No external service. Limit: not a full breach corpus.
- Verified-email gating: `REQUIRE_VERIFIED_EMAIL` guards document and ingestion routes with the `VerifiedUser` dependency (403 `EMAIL_NOT_VERIFIED`); auth/account routes stay available so users can verify. Production config validation requires it to be true.

## ADR-020 (2026-11-08): Orphan sweep
- Beat task `sweep-orphans` lists storage (`iter_objects`, with modification times), deletes objects in the generated key layout that have no document row and are older than `ORPHAN_GRACE_S`, and clears stale temporary uploads. The grace period prevents racing an upload that has written the object but not yet committed its row.

## ADR-021 (2026-11-08): Concurrency model
- Blocking work never runs on the event loop: Argon2 in threads behind a semaphore; hashing/validation on a dedicated executor; parsing in sandbox processes; a larger default thread pool; single-query hot paths (windowed list query, `get_owned_with_job`); notification emails as background tasks; uvicorn `--workers` for multiple cores.
- Guardrail test measures event-loop lag during a mixed burst of 12 users with production-strength Argon2 and fails if blocking calls appear (confirmed to fail against a mutant that hashes on the loop).
- Benchmark (`load_testing/concurrency_bench.py`, dev laptop, local services, one client process): throughput rose from 87 to 259 req/s with 3 API processes, and upload p50 fell from 1813 ms to 227 ms after the optimisations. Dev-machine numbers only (KI-022), not capacity claims.
