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

## Sessions (Phase 5; Bearer required, email must be verified when `REQUIRE_VERIFIED_EMAIL=true`)
| Method | Path | Body / query | Success | Errors |
|---|---|---|---|---|
| POST | /sessions | `{title?, agent_config?{mode single\|multi, top_k 1-30, rerank}}` | 201 `{id, title, agent_config, message_count, created_at, updated_at}` | 401, 403, 422, 429 |
| GET | /sessions | `limit` (1-100), `offset` | `{items, total, limit, offset}` newest activity first | 401, 403, 422 |
| GET | /sessions/{id} | | session | 401, 403, 404 |
| PATCH | /sessions/{id} | `{title?, agent_config?}` | session | 401, 403, 404, 422 |
| DELETE | /sessions/{id} | | 202 `{conversation_id, status:"deleting"}`; hidden at once, purged by the worker | 401, 403, 404 |
| GET | /sessions/{id}/messages | `after_seq`, `limit` (1-200) | `{items:[{id, seq, role, content, response, execution_id, created_at}], has_more}` | 401, 403, 404, 422 |

Another user's session is a 404 identical to an unknown id. Agent options are limited to `mode`, `top_k`, `rerank` (unknown fields are rejected). `POST /chat` and `POST /chat/stream` arrive in Phase 6; their turn lifecycle (409 `CONCURRENCY_CONFLICT` while a turn is running, `Idempotency-Key` semantics, 409 `IDEMPOTENCY_KEY_REUSED`) is implemented and tested in `ChatTurnService`.

## Memory and preferences (Bearer required; memory writes also need a verified email when required)
| Method | Path | Body / query | Success | Errors |
|---|---|---|---|---|
| GET | /memory | `kind?`, `limit` (1-100), `offset` | `{items, total, limit, offset}` | 401, 403, 422 |
| POST | /memory | `{content (<= MEMORY_MAX_CHARS), kind?, importance? 1-5}` | 201 memory | 409 duplicate, 422 (`sensitive_content`, `content_too_long`, `memory_limit_reached`), 429 |
| PATCH | /memory/{id} | `{content?, kind?, importance?}` | memory (re-indexed when content or kind changed) | 404, 409, 422, 429 |
| DELETE | /memory/{id} | | 204; hidden at once, vector and row removed by the worker | 404 |
| POST | /memory/reset | `{confirm: true}` | 202 `{status:"resetting", memories}` | 422 |
| POST | /memory/search | `{query, k?}` | `{results:[{memory, score}]}` relevant memories only | 422 |
| GET | /users/me/preferences | | `{memory_enabled (default false), memory_retention_days (default 365)}` | 401 |
| PATCH | /users/me/preferences | `{memory_enabled?, memory_retention_days? 1-3650}` | preferences | 401, 422 |

`/memory/search` previews what the agent would see and ignores the consent switch; the agent path itself uses memory only when `memory_enabled` is true.

## Chat (Phase 6; Bearer required, verified email when `REQUIRE_VERIFIED_EMAIL=true`)
| Method | Path | Body / headers | Success | Errors |
|---|---|---|---|---|
| POST | /chat | `{session_id, message (1..100000)}` + header `Idempotency-Key` (8-128 chars of A-Z a-z 0-9 _ . : -) | 200 `{session_id, execution_id, user_message_id, assistant_message_id, replayed, response: RAGResponse}` | 401, 403, 404, 409 (`CONCURRENCY_CONFLICT`, `IDEMPOTENCY_KEY_REUSED`, `TURN_FAILED`), 422, 429 (`RATE_LIMIT_EXCEEDED`, `MODEL_RATE_LIMITED` + Retry-After), 502 `MODEL_PROVIDER_ERROR`, 503 (`MODEL_NOT_CONFIGURED`, `PERSISTENCE_FAILED`, `SERVICE_UNAVAILABLE`), 504 `MODEL_TIMEOUT` |
| POST | /chat/stream | same | 200 `text/event-stream`: `start`, `status*`, `token*`, `citation*`, `complete` or `error` | the refusals above (before the stream opens) are plain JSON |

`RAGResponse` (schema_version "1.0", unknown fields rejected): `{answer, citations[{document_id, document_name, page_number, chunk_id, excerpt}], insufficient_evidence, clarification_needed, follow_up_questions, metadata{execution_mode, model_name, retrieval_used, reranking_used, answer_support supported|inference|not_found|none, cache_status hit|miss|bypass|error|none, llm_calls, degraded[], route}}`. Citations are built by the server from stored data; `[1]`, `[2]` markers in `answer` index into `citations` in order.
SSE events (JSON `data:` per frame, `event:` = `type`): `start{session_id, execution_id, user_message_id, replayed}`, `status{stage: retrieving|generating|repairing|verifying|saving}`, `token{index, text}` (slices of the validated, already saved answer; concatenated = `answer`), `citation{index, citation}`, `complete{assistant_message_id, response}`, `error{code, message, retryable, retry_after_s}`; `: keep-alive` comments every 15 s. After `start` exactly one terminal event (`complete` or `error`) follows. A client disconnect cancels the turn (the user message stays, no assistant message exists, the session is free). Replaying the key of a completed turn streams the stored answer (`start.replayed=true`, no `status`, no model call).
`degraded` values: `unverified_answer` (the safe fallback replaced an answer that could not be verified), `rewrite_fallback`, `memory_unavailable`, `rerank_timeout`, `rerank_unavailable`, `dense_unavailable`, `sparse_unavailable`.
