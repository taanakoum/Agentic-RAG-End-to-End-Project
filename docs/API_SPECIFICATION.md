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

## Not yet implemented
Users, documents, ingestion, sessions/chat, memory, feedback, evaluation endpoints (see docs/spec/06-api-errors-frontend.md).
