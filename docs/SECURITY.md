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

## Not implemented yet
Account deletion, breached-password check, audit log table, malware scanning of uploads, prompt-injection defenses (Phases 3-6).
