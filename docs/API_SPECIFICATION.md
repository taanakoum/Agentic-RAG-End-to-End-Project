# API Specification (as implemented)

Base path `/api/v1`. The live OpenAPI document is served at `/openapi.json` (disabled when `APP_ENV=prod`). Every response carries `X-Request-ID` (a well-formed incoming value is reused).

## Error contract
```json
{"error": {"code": "VALIDATION_ERROR", "message": "...", "details": [{"field": "password", "code": "password_too_short", "message": "..."}], "request_id": "..."}}
```
Validation errors never include submitted values. Codes used so far: VALIDATION_ERROR (422), MALFORMED_REQUEST, AUTHENTICATION_REQUIRED / INVALID_TOKEN / SESSION_REVOKED / INVALID_CREDENTIALS / INVALID_REFRESH_TOKEN (401, `WWW-Authenticate: Bearer`), FORBIDDEN, RESOURCE_NOT_FOUND (404), METHOD_NOT_ALLOWED (405), EMAIL_ALREADY_REGISTERED (409), PAYLOAD_TOO_LARGE (413), RATE_LIMIT_EXCEEDED (429, `Retry-After`), SERVICE_UNAVAILABLE (503), INTERNAL_ERROR (500). Exception classes for later phases exist in `app/core/exceptions.py`.

## Health (no auth)
| Method | Path | Result |
|---|---|---|
| GET | /health/live | 200 `{"status":"ok"}` |
| GET | /health/ready | 200/503 `{status, components:{postgres,redis,qdrant,storage}}` |
| GET | /health/workers | 200/503 `{status, components:{celery_workers}}` |

## Auth
| Method | Path | Body | Success | Errors |
|---|---|---|---|---|
| POST | /auth/register | `{email, password, display_name?}` | 201 `{id,email,display_name,created_at}` | 409, 422, 429, 503 |
| POST | /auth/login | `{email, password}` | 200 `{access_token, refresh_token, token_type:"bearer", expires_in}` | 401, 422, 429, 503 |
| POST | /auth/refresh | `{refresh_token}` | 200 token pair (old refresh token is invalidated) | 401, 422, 429, 503 |
| POST | /auth/logout | `{refresh_token}` | 204 (idempotent; revokes the whole session) | 422 |
| GET | /auth/me | Bearer | 200 user | 401 |
| GET | /auth/sessions | Bearer | 200 `[{id, created_at, last_used_at, user_agent, current}]` (own live sessions) | 401 |
| DELETE | /auth/sessions/{session_id} | Bearer | 204 | 401, 404 (also for other users' sessions), 422 |

Unknown JSON fields are rejected (422). Bodies over 1 MiB (configurable) get 413.

## Account security (all under /auth)
| Method | Path | Body | Success | Errors |
|---|---|---|---|---|
| POST | /password/change | `{current_password, new_password}` (Bearer) | 204; other sessions revoked | 401, 422, 429 |
| POST | /password/forgot | `{email}` | 202 always (same body for unknown emails) | 422, 429 |
| POST | /password/reset | `{token, new_password}` | 204; all sessions revoked | 400 `INVALID_RESET_TOKEN`, 422, 429 |
| POST | /email/verify/request | Bearer | 202 | 401, 429 |
| POST | /email/verify | `{token}` | 204 | 400 `INVALID_VERIFICATION_TOKEN`, 422, 429 |
| GET | /mfa/status | Bearer | `{enabled, recovery_codes_remaining}` | 401 |
| POST | /mfa/setup | Bearer | `{secret, otpauth_uri}` | 401, 409 `MFA_ALREADY_ENABLED` |
| POST | /mfa/confirm | `{code}` (Bearer) | `{recovery_codes: [...]}` (shown once) | 400 `MFA_NOT_PENDING`, 401 `INVALID_MFA_CODE`, 429 |
| POST | /mfa/verify | `{mfa_token, code}` (TOTP or recovery code) | login response with tokens | 401 `INVALID_MFA_TOKEN`/`INVALID_MFA_CODE`, 429 |
| POST | /mfa/disable | `{password, code}` (Bearer) | 204 | 401, 422, 429 |

`POST /auth/login` now returns `{mfa_required, access_token, refresh_token, token_type, expires_in, mfa_token}`; when `mfa_required` is true only `mfa_token` is set. `UserPublic` gained `email_verified`.

## Documents and ingestion (Bearer required; every route is scoped to the caller)
| Method | Path | Notes | Success | Errors |
|---|---|---|---|---|
| POST | /documents/upload | multipart field `file` (PDF) | 202 new `{document, job_id, duplicate:false}`; 200 when an identical file was uploaded before (`duplicate:true`, nothing re-processed) | 400 `DOCUMENT_EMPTY`/`DOCUMENT_INVALID_FORMAT`/`DOCUMENT_CORRUPTED`/`DOCUMENT_MISSING`, 413 `DOCUMENT_TOO_LARGE`/`PAYLOAD_TOO_LARGE`, 415 `DOCUMENT_UNSUPPORTED_TYPE`, 422 `DOCUMENT_ENCRYPTED`/`DOCUMENT_TOO_MANY_PAGES`/`VALIDATION_ERROR`, 429, 503 |
| GET | /documents | `limit` (1-100), `offset`, `status` | `{items, total, limit, offset}` | 401 |
| GET | /documents/{id} and /documents/{id}/status | | document incl. `status`, `page_count`, `chunk_count`, `ocr_status`, `ocr_pages`, `version`, `latest_job_id`, `error{code,message,recoverable}` | 404 |
| POST | /documents/{id}/reindex | completed or failed documents | 202 `{document_id, job_id, status}` (new version) | 404, 409 `REINDEX_NOT_ALLOWED` |
| DELETE | /documents/{id} | tombstone now, purge async | 202 `{document_id, status:"deleting"}` | 404 |
| GET | /ingestion/{job_id} | | `{id, document_id, version, status, attempts, error, created_at, started_at, finished_at}` | 404 |
| POST | /ingestion/{job_id}/retry | only failed + recoverable | 202 new job | 404, 409 `RETRY_NOT_ALLOWED` |

Other users' documents and jobs return 404, identical to a non-existent id. Document statuses: pending, validating, queued, processing, extracting, chunking, embedding, indexing, completed, failed. Only `completed` documents will be retrievable (Phase 4).

## Not yet implemented
Users, sessions/chat, memory, feedback, evaluation endpoints (see docs/spec/06-api-errors-frontend.md).

## Updates 2026-11-08
- `POST /documents/upload` accepts PDF, DOCX, TXT, MD and CSV (type verified from content). Rejections use the document error codes (`DOCUMENT_UNSUPPORTED_TYPE`, `DOCUMENT_INVALID_FORMAT`, `DOCUMENT_TOO_LARGE`, `DOCUMENT_EMPTY`, `DOCUMENT_CORRUPTED`, `DOCUMENT_ENCRYPTED`). Document responses include `source_format` and `parser`.
- `DELETE /auth/account` body `{password, code?}` (Bearer; `code` required when MFA is on) -> 202 `{status:"deleting"}`; errors 401, 422 (`incorrect_password`), 429, 503. All sessions end immediately.
- Document and ingestion routes return 403 `EMAIL_NOT_VERIFIED` when `REQUIRE_VERIFIED_EMAIL=true` and the email is unverified.
- Registration and password change/reset reject breached passwords (422 validation detail `password_breached`).

## Search (Phase 4; Bearer required, email must be verified when `REQUIRE_VERIFIED_EMAIL=true`)
| Method | Path | Body | Success | Errors |
|---|---|---|---|---|
| POST | /search | `{query (1..1000 chars), document_ids? (max 50), top_k? (1..100, capped by RETRIEVAL_MAX_TOP_K)}` | 200 `{query, mode, results[], degraded[], timings_ms{}}` | 401, 403 `EMAIL_NOT_VERIFIED`, 422 (`query_empty`, `query_too_long`, `too_many_documents`, schema), 429, 503, 504 `RETRIEVAL_TIMEOUT`, 500 `RETRIEVAL_FAILED` |

Each result is citation-ready: `{chunk_id, document_id, document_name, page_number, page_end, section_path, heading, text, scores{dense_rank, sparse_rank, dense_score, sparse_score, fused_score, rerank_score}}`.
The search covers only the caller's COMPLETED, non-deleted documents. `document_ids` narrows the scope; ids that are unknown, foreign or not completed are silently ignored (same response either way). `mode` is `hybrid_rerank` (or `hybrid` when `RERANK_ENABLED=false`). `degraded` lists handled soft failures: `dense_unavailable`, `sparse_unavailable`, `rerank_timeout`, `rerank_unavailable`.
