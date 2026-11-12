# Testing strategy

Core tests never call a paid or hosted model (`LLM_*` is blanked in `backend/tests/conftest.py`; the
model is a scripted test double) and use REAL PostgreSQL, Redis, Qdrant, a real Celery worker, real
sockets and the real Streamlit script. Only embedding and cross-encoder models are replaced by
deterministic test doubles in most tests (real-model tests are marked `real_model`).

## One command per category

`powershell -ExecutionPolicy Bypass -File scripts/test.ps1 -Category <name>` (start the native
services first: `scripts/dev_services.ps1 start`).

| Category | Selects | Tests (2026-11-12) |
|---|---|---|
| `unit` | everything under `backend/tests/unit` (no services) | 528 |
| `integration` | real services, excluding `docling` and `real_model` | 557 (incl. the 6 docling/real-model ones in the full run) |
| `e2e` | complete user workflows through the real stack | 91 |
| `security` | authentication, authorization, isolation, injection, privacy, abuse limits | 268 |
| `concurrency` | races, simultaneous users, idempotency, quotas | 61 |
| `failure` | dependency outages, crashes, timeouts, recovery | 99 |
| `frontend` | the Streamlit client and app | 46 |
| `all` | the full suite | 1085 |
| `quality` | `ruff format --check`, `ruff check`, `pyright` (strict; backend, frontend, evaluation, load_testing), migration upgrade/downgrade/upgrade | n/a |

Categories are assigned by file in one table (`_CATEGORY_FILES` in `backend/tests/conftest.py`); a
file may belong to several. The Docker smoke test is `scripts/verify_g1.ps1` (needs Docker, KI-014).

## Spec checklist -> where it is tested

| Spec item (docs/spec/07) | Tests |
|---|---|
| E2E A registration | `test_auth_api`, `test_frontend_ui` (register through the UI) |
| E2E B valid upload through indexing | `test_documents_pipeline` (real worker), `test_frontend_flows` |
| E2E C invalid files: missing, empty, wrong extension, renamed non-PDF, corrupted, oversized, encrypted, scanned, duplicate | `test_documents_api`, `test_documents_pipeline`, `unit/test_ingestion_units`, `unit/test_formats`, `unit/test_sandbox` |
| E2E D RAG chat with citations + reopen | `test_chat_e2e`, `test_chat_api`, `test_frontend_flows` |
| E2E E multi-agent | removed with Phase 7 (ADR-028) |
| E2E F memory across sessions + deletion | `test_memory_pipeline`, `test_memory_api`, `test_agent_graph` (consent), `test_frontend_flows` |
| E2E G two-user isolation incl. manipulated ids | `test_auth_ownership`, `test_sessions_api`, `test_retrieval`, `test_chat_lifecycle`, `test_frontend_api`, `test_frontend_flows` (forged UI state) |
| E2E H concurrency: separate/same conversation, duplicate submit, simultaneous streams, delete during run | `test_chat_turns`, `test_agent_graph`, `test_chat_lifecycle`, `test_concurrency` |
| E2E I SSE order, tokens, citations, errors, completion, cancellation, persistence | `test_chat_api`, `test_chat_e2e`, `test_frontend_api` |
| Security: SQLi | `test_auth_api::test_sql_injection_attempts` |
| Security: cross-user access | see G above; every router has an ownership test |
| Security: token misuse, expired, alg none, wrong type, wrong signature | `unit/test_security`, `test_auth_api` |
| Security: refresh reuse | `test_auth_api::test_refresh_token_reuse_revokes_whole_family` |
| Security: brute force | `test_auth_api` (per-account lockout, per-IP limit), `test_account_api` |
| Security: malformed PDFs, path traversal | `test_documents_api` (`../../` file name), `test_storage_contract` (key validation), sandbox tests |
| Security: prompt injection, tool access, data leakage | `test_agent_graph` (injected document, compromised model, other user's secret), `test_agent_eval`, `test_observability` (privacy scan of spans) |
| Security: HTTP hardening | `test_http_hardening` (headers on every response kind, CORS off/exact, HSTS only in prod) |
| Failure: PostgreSQL / Redis / Qdrant unavailable | `test_outages` (stable 503 on every endpoint kind, no leaks), `test_celery_and_health`, `test_auth_api` (rate limiter fails closed), `test_llm_cache` (cache fails open) |
| Failure: worker crash | `test_documents_pipeline` (process killed mid-ingest, reconciler recovers), `test_workers_down`, `test_orphans` |
| Failure: provider timeout / rate limit | `unit/test_llm_provider`, `test_agent_graph`, `test_llm_quota`, `test_agent_eval` |
| Failure: storage error | `test_storage_contract`, `test_documents_pipeline` |
| Failure: client disconnect | `test_chat_api` (real socket disconnect cancels the generation) |
| Quality commands | `scripts/test.ps1 -Category quality` |

## Rules that hold for every test

- No blanket suppressions: every `noqa`, `type: ignore` and `pyright: ignore` names its rule
  (audited 2026-11-12: none bare). Per-file ruff ignores are limited to asserts/fake secrets in tests
  and long lines in data-heavy scripts.
- Mutation checks guard the important tests: a property is only trusted once a deliberate defect
  made a test fail (results in docs/EXECUTION_LOG.md).
- Timing assertions are the weakest tests: see KI-045.
