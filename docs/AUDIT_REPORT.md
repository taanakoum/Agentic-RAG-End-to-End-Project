# Deployment readiness review

Date: 2026-11-14. Scope: repository packaging, backend/Streamlit deployment boundaries, database
transport and namespace, worker lifecycle, ownership/retrieval/citations, and verification of the
existing implementation. This is a source review and local verification, not a penetration test
or a claim of hosted production readiness. Phase 11 load testing is explicitly skipped.

## Findings and resolution

| ID | Severity | Finding | Resolution |
|---|---|---|---|
| DEP-01 | Critical for deployment | Unanchored `storage/` and `data/` gitignore rules excluded every storage adapter and the breached-password corpus from the published repository. Local tests could pass while a clean checkout could not import/run the app. | Anchored runtime exclusions to `/storage/` and `/data/`. Required application files now appear as untracked source for the owner's review. They must be included in the eventual owner-approved commit. |
| DEP-02 | High | Windows timeout handling trusted an unchecked `taskkill` invocation; the real timeout test took 120 seconds instead of less than 15. | Kill the sandbox child using its existing process handle (`Popen.kill`/TerminateProcess). Windows job objects already restrict the child to one process. All 33 sandbox tests passed after the change. |
| DEP-03 | High for hosted data | API/worker/migration/checkpointer connections did not consistently require authenticated TLS. | Added shared TLS settings. Production requires `verify-full`; asyncpg uses a verified SSLContext and psycopg/Alembic/LangGraph receive libpq TLS options. Tests cover hostname/CA checks, missing certificates and password escaping. Live Supabase certificate verification remains pending. |
| DEP-04 | High if deployed into exposed schema | Existing app tables have application-layer authorization, not Supabase Auth RLS policies. Putting these tables in Supabase's exposed `public` schema could bypass FastAPI via Data APIs. | Fresh-deployment SQL provisions a dedicated non-superuser role and private, non-exposed `rag` schema. Search path is applied to every DB client. No Supabase anon/service keys are given to the frontend. This does not secure an already populated public schema automatically. |
| DEP-05 | High for private-schema deletion | Checkpoint purge checked `public.checkpoints` even when queries used a different search path. It could skip deletion or try to delete missing tables. | Existence check now resolves `checkpoints` through the same search path as deletion. The private-schema integration scenario exercises initialization, graph persistence, thread deletion and orphan sweep. |
| DEP-06 | Medium | Docker used a fixed port and a multi-process default that duplicates heavyweight model memory on small hosts. | Respect Render's PORT, default to one process; Compose still explicitly controls its own process count. Added CA roots and the supplementary group needed to read Render secret files. Build/runtime remain unverified without Docker. |
| DEP-07 | Medium | Browser preflight could not authorize `Idempotency-Key`, and clients could not read request IDs/retry hints. | Exact configured origins can use the header; request IDs and Retry-After are exposed. CORS remains disabled by default. Real API tests cover allowed and denied origins. Streamlit itself uses server-to-server calls. |
| DEP-08 | Medium | Cloud frontend dependency discovery could select the root backend/ML lockfile; local Streamlit secret files lacked explicit git exclusions. | Added frontend-only requirements beside the entrypoint and ignored actual secrets.toml files. No credentials are in the frontend template. |
| DEP-09 | Medium | No Docker build-context exclusions for local runtime data and secrets. | Added `.dockerignore`; preserve backend source and password corpus while excluding root runtime data, environments, private notes, credentials and caches. |
| DEP-10 | Deployment constraint | Vercel does not provide the persistent Streamlit process; Render has no free Celery workers and blocks standard SMTP on free web instances. | Owner chose to keep Streamlit/all features. Documented Community Cloud and a clearly labelled paid Render reference. No feature removed, free deployment promised, account created, purchase made or deployment performed. |
| DEP-11 | Low (test only) | The disconnect test manually entered a DB session without closing it, producing a connection cleanup warning during the full run. | Use an async context manager. All 27 chat API tests passed with SQLAlchemy SAWarning promoted to errors; no warnings in that rerun. |

## Architecture and security review notes

- Interactive chat stays in async FastAPI/LangGraph. Celery handles ingestion and maintenance;
  PostgreSQL's outbox and persisted state drive recovery after broker/worker failure.
- API schemas remain separate from ORM models, and routes remain under `/api/v1`.
- Argon2id hashing runs off the event loop. PostgreSQL enforces normalized-email uniqueness;
  refresh rotation, MFA and verified-email gating remain intact.
- Resource lookup requires authenticated ownership and uses 404 for foreign resources. Qdrant
  retrieval, cache reload and vector maintenance apply owner filters. Memory is consented and scoped.
- Citation verification resolves model labels to retrieved authorized evidence and current readable
  document versions. It verifies source provenance/excerpts, not the truth of every generated claim.
- Conversation/checkpoint state stays in PostgreSQL, with same-thread concurrency controls and chat
  idempotency. All clients must use the same session-capable DB endpoint/schema in production.
- Upload parsing remains in child-process sandboxes; Linux resource limits still need container
  verification. The sandbox is not an independent filesystem/network security boundary.
- Frontend API tokens remain server-side. Remembered browser sign-ins use expiring revocable handles
  and are forgotten on frontend process restart; horizontal frontend scaling needs a separate design.

## Release blockers and limits

The fixes above are source changes awaiting owner review. The storage adapters and password corpus
are intentionally **uncommitted/untracked**, so a remote checkout is not repaired until the owner
includes them in an approved commit. The repository index has not been changed.

G11/G12 are not passed: Docker CLI is unavailable; no Linux image build, clean container lifecycle,
live Supabase S3/TLS test or cloud end-to-end walkthrough has run. The paid blueprint's resource
sizes are estimates; load/capacity is unmeasured by explicit instruction. Real SMTP delivery,
provider free quotas/pausing, backups/restores and cloud network restrictions need deployment-time
verification. Known key-rotation and licence actions remain in `KNOWN_ISSUES.md`.

See `VERIFICATION_REPORT.md` for actual commands/results and `HOSTING.md` for configuration and
the exact hosted verification checklist. No production-ready or fully-free claim follows from this audit.
