# 06 API, Errors, Pydantic, Streamlit

## Layering
Routers (HTTP only) -> services (business logic) -> repositories (DB) ; schemas (Pydantic) ; models (SQLAlchemy) ; dependencies (auth, db session, services). Never expose ORM models or internal fields. Separate Create/Update/Internal/Public schemas. Reject unknown fields where appropriate. Strict types, bounds, enums, validators.

## Endpoints (all under /api/v1)
Auth: POST auth/register, auth/login, auth/refresh, auth/logout; GET auth/me
Users: profile, preferences, account ops
Documents: POST documents/upload; GET documents; GET documents/{id}; DELETE documents/{id}
Ingestion: GET ingestion/{job_id}; POST ingestion/{job_id}/retry
Sessions: POST sessions; GET sessions; GET sessions/{id}; PATCH sessions/{id}; DELETE sessions/{id}
Chat: POST chat; POST chat/stream
Memory: GET memory; POST memory; PATCH memory/{id}; DELETE memory/{id} (+ reset)
Feedback: POST feedback
Ops: GET health (liveness), GET ready (readiness incl. PG/Redis/Qdrant/workers/storage)
Protected admin/evaluation endpoints. Keep OpenAPI accurate; document final API in API_SPECIFICATION.md.

## Error contract
```json
{"error":{"code":"DOCUMENT_INVALID_FORMAT","message":"The uploaded file is not a valid PDF.","details":[],"request_id":"..."}}
```
Validation errors: code VALIDATION_ERROR with `details:[{field,code,message}]`.
Custom exceptions: AuthenticationError, AuthorizationError, ResourceNotFoundError, DuplicateResourceError, DocumentValidationError, DocumentParsingError, IngestionError, RetrievalError, AgentExecutionError, ModelProviderError, RateLimitExceededError, ConcurrencyConflictError, ExternalServiceUnavailableError. Registered centrally; no scattered `except Exception`.
Status map: 400 malformed, 401 credentials, 403 permission, 404 not found, 409 duplicate/conflict, 413 too large, 415 unsupported type, 422 validation, 429 rate limit, 500 internal, 503 dependency down, 504 upstream timeout.
Every error: stable code, safe specific message, request_id, secure technical logging, no stack traces to users.

## Streamlit (talks ONLY to FastAPI)
Auth (register/login/logout/expiry handling). Chat with streaming, new/list/select/rename/delete conversations, history, citations + document references, processing indicators, feedback (thumbs + comment). Documents: upload with file info/progress/validation feedback, status, failure details, retry, list, delete. Agent config: single/multi, safe options (model, top-k, rerank) only. Memory: view/edit/delete/reset. Error presentation: field-level highlights; distinguish missing fields, invalid credentials, unsupported vs corrupted PDF, size limits, outages, processing failures, rate limits, session conflicts; retry buttons only where safe. Never report success before backend confirms. No direct DB access.
