# Security (as implemented through Phase 2)

Requirements: `docs/spec/02-auth-security.md`. This file records what exists and its known limits. Update it every phase.

## Passwords
- Argon2id (argon2-cffi). Defaults: memory 64 MiB, time 3, parallelism 4 (measured on the dev laptop: login ~90 ms, register ~185 ms). Prod config validation rejects memory < 19456 KiB or time < 2.
- Hashing runs in worker threads behind a semaphore (`max_concurrent_hashes`, default 4) so it neither blocks the event loop nor lets concurrent requests exhaust memory.
- Rehash on successful login when parameters change (tested).
- Unknown accounts still burn one Argon2 verification (dummy hash) to equalize timing.
- Policy: 12-128 characters, not blank, at least 5 distinct characters, not in a small common list, must not contain the email local part (when 4+ chars). This is a deliberately modest policy; there is no breached-password check.

## Email and uniqueness
- One shared `normalize_email` (syntax check without DNS, Unicode NFKC, lowercase). `users.email_normalized` has a PostgreSQL UNIQUE constraint and a CHECK that it is lowercase. The constraint is the final authority: `IntegrityError` becomes 409 `EMAIL_ALREADY_REGISTERED`. Proven by a 24-way concurrent registration test and by a test that disables the application pre-check.
- Registration reveals whether an email is taken (409). The spec requires 409; this is a conscious enumeration trade-off, mitigated by the per-IP registration limit.

## Tokens
- Access token: HS256 JWT, 15 min. Validated: signature with algorithm list `["HS256"]` only, exp, iat (not in the future), iss, aud, required claims (`exp iat iss aud sub type sid jti`), `type == "access"`. Tested against alg=none, other HMAC algorithms, wrong secret/issuer/audience/type, missing claims.
- The access token carries `sid` (session id = refresh-token family). Every authenticated request checks in PostgreSQL that the user is active and the session still has a live refresh token. So logout, session revocation and reuse detection take effect immediately, not after the 15 minutes.
- Refresh token: 384-bit random opaque string, only its SHA-256 is stored. Rotation on every use. Replaying an already-used token revokes the whole family. Two concurrent refreshes of the same token serialize on a row lock; exactly one wins and the loser triggers family revocation (strict by design, so a legitimate client that double-submits has to log in again).
- JWT signing secret comes from `JWT_SECRET` (min 32 chars, enforced). There is no key rotation mechanism yet (single secret).

## Rate limiting and brute force
- Redis fixed-window counters (atomic Lua INCR+EXPIRE, always with a TTL). Keys are SHA-256 digests; no emails or IPs in Redis in clear text.
- Login: per-IP (20/min), plus per-account failure counter (5 failures / 15 min) that locks the account even for the correct password until the window expires. The counter is keyed on the submitted identity whether or not the account exists. A successful login resets it. Registration: 10/hour per IP. Refresh: 60/min per IP. All configurable.
- Fails closed: if Redis is unavailable, login/register/refresh return 503.
- Known limits: fixed windows allow up to 2x bursts at a window boundary; per-account lockout lets an attacker deliberately lock out a known victim for 15 minutes (a usability/security trade-off); behind a proxy the client IP depends on `--proxy-headers`/forwarded-allow-ips being configured correctly.

## Errors, logging
- Central error contract (`{"error": {code, message, details, request_id}}`), stable codes. Validation errors omit the submitted values so a rejected password is never echoed. Unhandled exceptions return a generic 500 with no stack trace. Every response carries `X-Request-ID`.
- Structured JSON logs (prod) with request IDs; keys containing password/token/secret/authorization/api_key/cookie are redacted. A test verifies that passwords and tokens never appear in log output during the auth flows.

## HTTP hardening
- Request body limit (1 MiB default) enforced from Content-Length and while streaming; 413 with the standard error body. CORS is off unless `CORS_ORIGINS` is set. `/docs` and `/openapi.json` are disabled when `APP_ENV=prod`.

## Isolation
- Identity is derived only from the validated token. `require_owner` returns 404 (not 403) for another user's or a missing resource; tested on login sessions (list/revoke) including the attacker case and a mutation check (removing the ownership check makes the test fail).
- Not yet covered (arrives with the resources): documents, vectors, conversations, checkpoints, memories, jobs.

## Account security (added after Phase 2)
- Password change (needs the current password; wrong-password attempts rate limited 5/15 min; other sessions revoked) and password reset by emailed single-use token (1 h; all sessions revoked; token survives a rejected weak password; concurrent use succeeds once). `forgot` never reveals whether an account exists.
- Email verification by single-use token (24 h). `email_verified` is exposed on the user; nothing is gated on it yet.
- TOTP MFA with encrypted seed, replay protection, recovery codes and a separate short-lived challenge token. See ADR-010.
- JWT signing key rotation with `kid` (ADR-010). Key storage is environment variables only (no KMS/vault).
- Limits: the email file backend writes tokens to disk (dev/test only); SMTP credentials live in env; recovery codes carry ~50 bits and rely on the 5-attempt rate limit; TOTP seeds are only as safe as `MFA_ENCRYPTION_KEY` (rotating it invalidates enrolled seeds; no re-encryption tool yet).

## Uploads and documents (Phase 3)
- Independent checks in order: file name present, `.pdf` extension (415), declared MIME (415), size (413), `%PDF-` magic bytes within the first 1 KiB (400), parser opens it (400 corrupted/malformed), not encrypted (422), page count (422). A `.pdf` name or `application/pdf` header alone never makes a file valid; a real PDF with the wrong extension or MIME is refused with 415. Parser inspection runs in a worker thread under a timeout. Text extraction and OCR run only in the Celery worker.
- Storage keys are generated (`<user>/<document>/original.pdf`); user filenames are only a sanitized display name (path components and control characters removed, 255 chars). Local storage also verifies the resolved path stays inside the root.
- Integrity: the SHA-256 computed on upload is compared with the bytes actually stored, and again by the worker before extraction (`STORAGE_INTEGRITY`).
- Isolation: every document/job query filters by the authenticated user; other users' ids behave like missing ids (404); a re-upload by another user is a normal 202 so existence is never revealed; workers re-check that job and document belong to the same user; every Qdrant maintenance filter carries user_id and document_id; points carry `user_id` as a tenant-indexed field for Phase 4 query filters.
- Limits (configurable): PDF size 25 MiB, 300 pages, upload rate 30/hour per user, request body cap = PDF limit + 1 MiB on the upload route only, OCR pages and DPI.
- Not covered: malware scanning, sandboxing the PDF parser in a separate process/container (pypdf runs in-process in a thread; a hostile PDF can still burn CPU until the timeout/worker time limit), encryption of stored objects at rest.

## Not implemented yet
Account deletion, breached-password check, audit log table, malware scanning of uploads, prompt-injection defenses (Phases 3-6).

## Added 2026-11-08 (parsing sandbox, formats, account lifecycle)
- Parsing sandbox (ADR-016): timeouts, memory cap, no process spawning (Windows), scrubbed environment. Tested on Windows against decompression bombs, self-referencing page trees, XXE, entity bombs and zip bombs. Linux limits are implemented but unverified (KI-020). No malware scanning.
- Uploads are validated by content for PDF, DOCX, TXT, MD, CSV (ADR-018); extension/MIME alone is never trusted. Object keys are generated, never derived from filenames.
- Account deletion (`DELETE /auth/account`): password plus MFA code, immediate lock-out, asynchronous purge of vectors, files and rows (ADR-019).
- Breached-password check: offline list; rejects known passwords and trivially decorated variants.
- `REQUIRE_VERIFIED_EMAIL`: document and ingestion routes require a verified email; production config refuses to start without it.
- Logging: the console renderer uses plain tracebacks (rich tracebacks print local variables, which can include secrets); regression test.
- JWT key rotation (`kid`) exists, so the earlier "no key rotation" note above is out of date.
- Still open: admin-forced MFA (KI-011), fixed-window rate limits (KI-012).

## Retrieval authorization (Phase 4)
- Three layers, all tested (and mutation-checked): (1) PostgreSQL decides which documents the caller may read (owned, `completed`, not deleted; optional narrowing by `document_ids`); (2) EVERY Qdrant query (dense and sparse) carries `user_id` AND the allowed `document_id` set in its filter, and each returned payload's `user_id` is re-checked; (3) after the search, hits are re-validated against PostgreSQL (deleted, re-indexed or no longer completed documents are dropped) before anything is returned.
- Tombstoned documents disappear from search instantly, before their vectors are purged. Documents that are being (re)processed are not searchable.
- Unknown, foreign and unreadable document ids are indistinguishable (empty result, same status).
- Search is rate limited per user (`RL_SEARCH_LIMIT` per `RL_SEARCH_WINDOW_S`), fails closed if Redis is down, and bounded: query length, scope size, top-k, per-stage timeouts.
- Retrieved text is untrusted input for the LLM (prompt-injection handling arrives with generation, Phase 6); search results never contain storage keys or user ids.
- Known limit: the Qdrant filter lists the readable document ids; users with thousands of documents produce large filters (bounded by `RETRIEVAL_MAX_SCOPE_DOCUMENTS` only when the caller narrows). Revisit with a status payload field if this becomes a bottleneck.

## Sessions, chat state and memory (Phase 5)
- Every conversation, message, execution and memory query is scoped by the owning user; foreign and deleted ids return the same 404 as unknown ones (tested, mutation-checked). `thread_id` is never exposed.
- Concurrency control is in the database (row lock, unique sequence, one running execution per conversation, per-conversation idempotency keys, leases): see ADR-023. Stale writers cannot overwrite answers or checkpoints.
- Deletion removes messages, executions and LangGraph checkpoints; account deletion also removes memories and their vectors. Reconciliation republishes lost purges; an orphan-checkpoint sweep removes late writes.
- Memory is opt-in (`memory_enabled` defaults to false), never retroactive, retention-limited, refuses obviously sensitive data (heuristic, ADR-025), is retrieved by relevance only, and every vector hit is re-validated against PostgreSQL. Users can view, edit, delete and reset their memories.
- Rate limits: session creation, memory writes (per user), search (per user). Agent options are an allow-list (`mode`, `top_k`, `rerank`); system prompts, tools and model selection cannot be set by users.
- Known limits: the memory sensitivity filter is heuristic; memory content is plain text in PostgreSQL (no field-level encryption); consolidation is rule-based until an LLM extractor is added (KI-026).
