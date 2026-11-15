# Hosting preparation: Streamlit, Render and Supabase

Prepared 2026-11-14. **No deployment, accounts, purchases or commits were made.** Phase 11
(load testing) is skipped at the owner's request. Docker and live cloud verification remain open.
The owner chose to retain Streamlit and every feature after the free-tier constraints were explained.

## Feasibility and the zero-dollar boundary

| Component | Requested host | Outcome with existing features |
|---|---|---|
| Streamlit frontend | Vercel free | No supported deployment configuration for this long-running Streamlit server. Use Streamlit Community Cloud instead. |
| FastAPI API | Render free | Free web hosting exists, but the ML footprint is unmeasured there, it sleeps, and standard SMTP ports are blocked. An API-only deployment is not the complete app. |
| Celery worker and beat | Render free | No free worker instances; ingestion, deletion, outbox recovery and memory maintenance need these services. |
| PostgreSQL | Supabase free | Suitable for a small trial within the project's quotas; use TLS and a private schema. |
| Original uploads | Supabase Storage | Existing S3 adapter can target the private bucket; live compatibility still needs verification. |
| Redis and Qdrant | Additional services | Both are mandatory. Supabase PostgreSQL alone does not replace either. Choose hosted instances or self-host on existing hardware. |

This is not a fully free hosted deployment. The runnable full-feature reference in
`deploy/render.yaml` explicitly uses **paid API/worker/scheduler plans and two paid model-cache
disks**. Do not apply it if the budget is zero. `autoDeployTrigger: "off"` disables subsequent
automatic deployments, not initial creation or billing. There is deliberately no `vercel.json`
claiming Streamlit can run as a Vercel Function.

Vercel's [Python runtime](https://vercel.com/docs/functions/runtimes/python) provides functions;
its [runtime limits](https://vercel.com/docs/functions/runtimes) do not provide an indefinitely
running Streamlit process. This hosting conclusion is an architectural inference; no Vercel
deployment was attempted. Render documents its [free service limitations](https://render.com/docs/free)
and [which service types have no free instance](https://render.com/docs/your-first-deploy).

For a strict zero-dollar budget while keeping all features, continue to run the full backend on
existing hardware using [LOCAL_SETUP.md](LOCAL_SETUP.md). A public frontend still needs a secure,
reachable HTTPS backend; exposing localhost safely is separate work. No tunnel is configured here.

## Supabase: a fresh database and private object bucket

1. Create/select a project in a region near the backend. Review its free plan limits and pause policy
   in the dashboard. This guide provisions a **fresh application installation**; it does not copy
   local users, documents or checkpoints.
2. Review and run [`deploy/supabase.sql`](../deploy/supabase.sql) once in the SQL Editor. It creates
   a non-superuser `rag_app` login with no password and a private `rag` schema owned by that role.
   Set a unique role password using your administrative tooling. The script intentionally has no
   usable default password and fails if its role/schema already exists.
3. Keep `rag` **out of the API's Exposed schemas list**, and do not grant `anon` or `authenticated`
   access to it. Authentication continues through FastAPI; this app does not use Supabase Auth.
   The app's tables include password hashes and refresh-token records: do not migrate them into
   the default exposed `public` schema. See [Supabase custom schemas](https://supabase.com/docs/guides/api/using-custom-schemas).
4. In **Connect**, choose **Session pooler** and copy the actual hostname. Configure
   `POSTGRES_USER=rag_app.<PROJECT_REF>`, `POSTGRES_PORT=5432`, `POSTGRES_DB=postgres`,
   `POSTGRES_SCHEMA=rag` and the role password. Do not guess the pooler hostname.
   Do **not** use transaction mode on port 6543: the checkpointer uses session advisory locks,
   prepared statements and pipelining. Session pooling also works over IPv4.
5. Enable SSL enforcement in Supabase. Download the project's server CA certificate and add it
   to Render as a secret file named `supabase-ca.crt`. Use `POSTGRES_SSLMODE=verify-full` and
   `POSTGRES_SSLROOTCERT=/etc/secrets/supabase-ca.crt`. Certificate and hostname verification
   apply to the API, workers, Alembic and LangGraph. A missing certificate fails startup instead
   of falling back to plaintext. Details: [database connections and TLS](https://supabase.com/docs/guides/database/connecting-to-postgres).
6. Create a **private** Storage bucket `rag-documents`. Enable S3 access and generate separate
   S3 access/secret keys. Copy the project's displayed endpoint and region into `S3_*`; neither
   the Supabase publishable key nor an Auth JWT is an S3 secret key. The backend's existing
   S3 adapter provides upload, read, delete, listing and multipart handling.
   See [S3 credentials](https://supabase.com/docs/guides/storage/s3/authentication).

Only backend hosts receive database, S3, Redis, Qdrant, SMTP, JWT, MFA and LLM credentials.
Supabase S3 credentials are privileged; never put them in the Streamlit secrets panel or browser.
Do not change `POSTGRES_SCHEMA` on an existing installation without a separate data migration.

## Render: full-feature paid reference

The API and inference worker need much more memory than a small web-only Python app. The reference
uses `pro` for the API, `pro plus` for one worker, and `starter` for beat; these are **unmeasured
starting configurations**, not demonstrated capacity or a cheapest-plan claim. Docling, OCR,
embeddings and cross-encoder reranking remain enabled. API/worker caches have separate disks.
The scheduler's local schedule file is expendable; durable work remains in PostgreSQL's outbox.

Before applying any paid configuration:

1. Supply a reachable Redis-compatible broker and Qdrant cluster. The existing local services are
   fine for development. For hosted services, review free-tier quotas, durability, idle suspension
   and network access; no provider account has been selected or created here. Retain Qdrant's
   owner filters and the same embedding model/dimension on API and worker.
2. Configure an SMTP provider with a verified sender. Production still requires email verification;
   it has not been disabled to work around free hosting. Render free blocks outbound 25/465/587;
   an HTTPS email adapter is not implemented. Paid Render also blocks port 25, so use 587 with
   STARTTLS (the template) or 465 with SSL and `SMTP_STARTTLS=false`.
3. Create the **`rag-production` environment group** manually from
   [`deploy/backend.env.example`](../deploy/backend.env.example), replacing every `REPLACE` value.
   Add the CA certificate as a secret file to that group. Group variables and files are shared
   by the API, worker and beat; do not generate separate JWT or MFA keys for each service.
   See [Render environment groups](https://render.com/docs/configure-environment-variables).
4. Generate fresh secrets locally (do not paste output into commits or test reports):

   ```bash
   python -c "import secrets; print(secrets.token_urlsafe(48))"
   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```

5. Set `APP_BASE_URL` to the exact final Streamlit HTTPS origin: account reset and verification
   links use it. Choose current LLM/provider settings and budgets appropriate for your account.
6. Review [`deploy/render.yaml`](../deploy/render.yaml) and the dashboard's complete price estimate.
   When deployment is separately authorized after code review, select that file as the Blueprint
   path. Paths inside it refer to the repository root. Keep automatic deployments off.
7. The API's paid **pre-deploy command** runs `alembic -c alembic.ini upgrade head`. For initial
   deployment, wait for that migration before manually starting/redeploying worker and beat.
   Blueprint services can start in parallel: worker/beat do not imply migration ordering.
   For later releases deploy migrations/API first, then the matching worker and beat version.
8. Run exactly **one beat instance**. The worker is a separate Celery process with concurrency 1.
   Interactive chat remains async FastAPI/LangGraph, never Celery.

The Dockerfile binds `0.0.0.0:${PORT:-8000}` with one API process by default. It includes system
CA roots and keeps an unprivileged user; supplementary group 1000 permits access to Render's
mounted secret files ([Render Docker secrets](https://render.com/docs/docker-secrets)).
The model directories under `/data` are writable by the app. No backend credentials are copied
into the image. Render's [Blueprint reference](https://render.com/docs/blueprint-spec) describes
the Docker command and pre-deploy fields. The template was checked against Render's published
JSON Schema; that does not verify a build, service startup or account-level settings.

## Streamlit Community Cloud: free frontend alternative

After source review and an owner-approved push to GitHub:

1. Select the repository, reviewed branch and main file **`frontend/app.py`**.
2. In Advanced settings choose **Python 3.13**, matching development.
3. Add the one root-level secret from [`.streamlit/secrets.toml.example`](../.streamlit/secrets.toml.example):

   ```toml
   FRONTEND_API_URL = "https://YOUR-API.onrender.com/api/v1"
   ```

4. Community Cloud searches the entrypoint directory first, so `frontend/requirements.txt` installs
   only Streamlit, httpx and the QR generator (versions aligned with `uv.lock`), rather than the
   root backend/ML dependencies. The repository root remains the working directory and the existing
   `.streamlit/config.toml` theme is reused.
5. Confirm the public frontend origin matches `APP_BASE_URL` on the backend. Streamlit calls FastAPI
   server-to-server, so CORS can stay empty. If a browser client is added later, allow only its exact
   origin via `CORS_ORIGINS` (a JSON list); the API now allows `Idempotency-Key` and exposes request
   IDs and retry hints.

Accounts, chat, upload/progress/retry, citations, memory and account controls retain their existing
UI. API tokens stay on the Streamlit server. Its opt-in remembered sign-ins are process-local and
are forgotten on restart or sleep; cloud hosting does not turn them into durable sessions.
Follow the official [deployment guide](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/deploy)
and [dependency discovery rules](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/app-dependencies).

## Verification before calling the deployment usable

First run the local Docker gate in [DEPLOYMENT.md](DEPLOYMENT.md). It remains unverified on this
machine. Do not infer Linux sandbox behavior from the Windows tests.

After a separately authorized hosted deployment, verify:

```bash
curl --fail https://YOUR-API.onrender.com/api/v1/health/live
curl --fail https://YOUR-API.onrender.com/api/v1/health/ready
curl --fail https://YOUR-API.onrender.com/api/v1/health/workers
```

Readiness writes/reads/deletes a temporary storage probe and checks PostgreSQL, Redis and Qdrant.
Worker health checks Celery connectivity; neither check proves beat is scheduling correctly.
Check beat logs and processing of a persisted outbox event. Confirm the DB migration version is
`0010` in schema `rag` and that TLS connections are shown in `pg_stat_ssl`.

In the hosted UI: register, receive/verify email, log in, upload a PDF, wait for Ready, ask a
question, inspect verified citations, reopen the conversation, test consented memory, verify reset
email, and test MFA. Use a second account to check isolation. Restart API/worker and confirm that
objects, conversations and checkpoints survive; delete a document and check its rows/vectors/object
are removed. Test enough file sizes to exercise multipart uploads and confirm the Supabase S3
orphan/multipart sweep is supported. This is still pending against real Supabase, not proven by moto.

Record actual results in `EXECUTION_LOG.md`. There is no deployment URL to report yet.
See [AUDIT_REPORT.md](AUDIT_REPORT.md) and [VERIFICATION_REPORT.md](VERIFICATION_REPORT.md).
