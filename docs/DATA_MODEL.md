# Data Model (as implemented; migration head 0002)

PostgreSQL is the source of truth. Constraint names follow the naming convention in `backend/app/database/base.py`.

## users (0002)
| Column | Type | Notes |
|---|---|---|
| id | uuid PK | `gen_random_uuid()` default (also generated app-side) |
| email | varchar(320) | as entered (trimmed) |
| email_normalized | varchar(320) | UNIQUE `uq_users_email_normalized`; CHECK `ck_users_email_normalized_lower` (= lower(...)) |
| password_hash | text | Argon2id encoded hash |
| display_name | varchar(100) null | |
| is_active | boolean | default true |
| created_at, updated_at | timestamptz | server defaults |

## refresh_tokens (0002)
| Column | Type | Notes |
|---|---|---|
| id | uuid PK | |
| user_id | uuid FK users(id) ON DELETE CASCADE | index `ix_refresh_tokens_user_id` |
| family_id | uuid | one family per login; equals the access-token `sid`; index `ix_refresh_tokens_family_id` |
| token_hash | varchar(64) | UNIQUE; SHA-256 hex of the opaque token (the token itself is never stored) |
| issued_at, expires_at | timestamptz | |
| revoked_at, revoked_reason | timestamptz null, varchar(32) null | reasons: rotated, logout, reuse_detected, expired, user_revoked, user_inactive |
| user_agent | varchar(256) null | informational |

A session is "live" while its family has a token that is unrevoked and unexpired.

## Planned (later phases)
documents, document_versions, ingestion_jobs, outbox_events, conversations, messages, LangGraph checkpoint tables, memories, preferences, feedback, evaluation_results, audit_events.

## Added in migration 0003
- `users.email_verified_at timestamptz null`.
- `one_time_tokens(id, user_id FK cascade, purpose, token_hash UNIQUE, expires_at, used_at, created_at)`, index (user_id, purpose). Purposes: `password_reset`, `email_verify`.
- `user_mfa(user_id PK/FK cascade, secret_encrypted, confirmed_at, last_used_step bigint, created_at)`.
- `mfa_recovery_codes(id, user_id FK cascade, code_hash UNIQUE, used_at, created_at)`.

## Added in migration 0004 (Phase 3)
- `documents(id, user_id FK cascade, filename, content_hash sha256, size_bytes, mime_type, storage_key, status CHECK, error_code, error_message, error_recoverable, page_count, version, ocr_status, ocr_pages, chunk_count, embedding_model, created_at, updated_at, completed_at, deleted_at)`. Partial UNIQUE index `uq_documents_user_content_hash_live (user_id, content_hash) WHERE deleted_at IS NULL`; index on (user_id, created_at) and on tombstones.
- `ingestion_jobs(id, document_id FK cascade, user_id FK, version, status CHECK(queued|processing|succeeded|failed|superseded), attempts, last_error, created_at, updated_at, started_at, finished_at)`.
- `outbox_events(id bigserial, kind (ingest_document|purge_document), payload jsonb, created_at, sent_at, attempts, last_error)`; partial index on unsent rows.
- Object keys: `<user_id>/<document_id>/original.pdf` (generated, never from filenames).
- Qdrant collection layout: see ADR-012.
