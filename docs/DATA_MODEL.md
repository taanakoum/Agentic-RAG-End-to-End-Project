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
