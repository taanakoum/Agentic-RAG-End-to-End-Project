# 02 Authentication, Isolation, Security

## Passwords
Argon2id via a maintained library; sensible params; rehash-on-verify when params change; secure password change. Never: plaintext, MD5, plain SHA-256, custom crypto, logging passwords, returning hashes. (SHA-256 is for file dedup only.)

## Registration
Validate email format, normalize via one shared function, store indexed `email_normalized`. Enforce password policy server-side. Display name optional. **PostgreSQL UNIQUE constraint/index on normalized email is the final authority.** Catch IntegrityError within the transaction -> HTTP 409. Concurrent duplicate registration must be tested. Same principle for every uniqueness requirement.

## Login / tokens
- Access tokens (short-lived) + refresh tokens with **rotation**, **revocation**, logout invalidation. Store refresh tokens hashed. Reuse of a revoked refresh token must be rejected (consider revoking the whole family).
- JWT: restricted algorithm list, validate exp/iat/iss/aud/type/sub, secure key from env. Never in URLs or logs.
- Generic login failure messages (no account enumeration). Per-IP and per-account brute-force protection via Redis rate limiting.
- Streamlit: keep credentials server-side in session state; do not put long-lived tokens in URLs/logs/browser-accessible storage; document limitations.

## Multi-user isolation (mandatory from day one)
Isolated per user: documents, versions, Qdrant vectors, conversations, messages, LangGraph threads/checkpoints, memories, jobs, feedback, preferences.
- Identity always derived from the validated auth context; never from a frontend-supplied user_id.
- Ownership checks in services, repository queries, Qdrant filters, checkpoint access, Celery tasks, object storage.
- Every Qdrant query carries user_id (and document filters) BEFORE content reaches the LLM.
- Return 404 (not 403) for other users' resources; never reveal existence via errors or dedup behavior.
- Test IDOR, guessed IDs, manipulated thread IDs, cross-user background-task access.

## Prompt injection / agent safety
Documents and retrieved text are untrusted data. Separate trusted system instructions from retrieved content (delimited, labeled as data). Document text must not override policy or authorization. Tool calls validated against explicit allowlists + user permissions; no unrestricted execution; tools receive the authenticated user context, not model-chosen identities.

## Other defenses
SQL injection (parameterized only), path traversal (never build paths from user filenames; generated object keys), oversized requests (body limits), malicious PDFs (limits, sandbox/timeouts, optional malware scan), resource exhaustion, CORS correctness, sensitive-data exposure, secrets only in env / secrets manager, `.env.example` with no real values.

## Rate limits (Redis, per-user and per-IP, structured 429)
Stricter on: registration, login, uploads, LLM operations, embedding generation, memory updates.

## Limits to make configurable
HTTP body size, PDF size, PDF page count, chat input length, context size, retrieval candidates, rerank batch, LLM output tokens, background job time.

## Logging hygiene
Never log passwords, tokens, API keys, or unnecessary document contents. Redaction + retention policy.

## Auth tests required
Register ok; missing fields; invalid email; weak password; duplicate email; concurrent duplicate; login ok; bad password; unknown account; expired access; expired refresh; revoked-refresh reuse; logout; unauthorized access; hash never in DB plaintext or API responses.
