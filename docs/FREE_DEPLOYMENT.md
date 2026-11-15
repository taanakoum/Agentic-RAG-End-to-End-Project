# Free deployment (branch `free-tier`): everything on free managed services

This variant runs entirely on free services, with nothing on your own computer once it is set up.
It is a *reduced* version of the full platform on `main`; docs/DEPLOYMENT_OPTIONS.md explains why
two variants exist. Provider limits below were read from the providers' pages on 2026-11-15.

## What runs where

| Part | Service (free plan) | Notes from the provider |
|---|---|---|
| Web UI | [Streamlit Community Cloud](https://docs.streamlit.io/deploy/streamlit-community-cloud/manage-your-app) | 690 MB-2.7 GB memory; sleeps after 12 h without traffic, any visitor can wake it |
| API + background tasks | [Render free web service](https://render.com/docs/free) | 512 MB, 0.1 CPU; spins down after 15 min without traffic (~1 min to wake); 750 free hours per month; no SMTP |
| Rate limits, caches | Render free Key Value | in memory only; lost on restart (acceptable: nothing critical lives there) |
| Database | Supabase free PostgreSQL | private `rag` schema, verified TLS (deploy/supabase.sql); free projects can be paused when inactive (check the current policy in the dashboard) |
| Uploaded files | Supabase Storage (S3 endpoint) | private bucket |
| Vectors | [Qdrant Cloud free cluster](https://qdrant.tech/documentation/cloud/create-cluster/) | 1 GB RAM, 0.5 vCPU, 4 GB disk; suspended after 1 week unused |
| Answers and embeddings | Gemini API | free quotas are shown in AI Studio, not published as fixed numbers |

A scheduled GitHub Action (`.github/workflows/keepalive.yml`) calls the API's readiness check every
three days so Supabase and Qdrant are not paused for inactivity; it runs on GitHub, not your laptop.

## What is different from `main`

| Feature | `main` (full) | `free-tier` |
|---|---|---|
| Background work | Celery worker + beat | the same outbox and idempotent tasks, run by a thread inside the API (ADR-042) |
| PDF parsing | Docling (layout, tables), OCR for scanned pages | text layer only (pypdf); scanned pages are not read |
| Dense embeddings | local `bge-small` (ONNX) | Gemini `gemini-embedding-001`, 768 dimensions (document text goes to Google) |
| Re-ranking | cross-encoder | none; hybrid or dense retrieval, chosen by evaluation (below) |
| Email | verification and password-reset emails | none (free hosts block SMTP); **sign-up needs an invite code** you hand out |
| Password reset | by email | not available: a user who forgets the password signs up again with the invite code |
| Availability | always on | first visit after idle waits ~1 min while the API wakes (the UI shows a message) |
| Everything else | | same: accounts, 2FA, uploads with live progress, cited answers verified by the server, conversations, memory, account deletion, per-user isolation |

## Measured on the development laptop (2026-11-15)

The free configuration was run locally against real PostgreSQL (a private schema, as on Supabase),
Redis and Qdrant, with the real Gemini API, one API process and **no Celery worker or beat**, and
driven through a real browser (`scripts/browser_tour.py --invite ...`). Memory is Windows working set,
a rough proxy for Linux RSS on Render.

| Run | Result | Memory of the whole backend |
|---|---|---|
| Hosted embeddings, "Attention Is All You Need" (15 pages, 52 passages), 2 questions | invite sign-up, processed in 6.5 s, both answers correct with verified citations | 344 MB total after the run; API process peak 322 MB |
| Hosted embeddings, NIST SP 800-63B (80 pages) + DOCX | stopped by Gemini's free embedding quota ("You exceeded your current quota"), see below | API process peak 336 MB; sum of all process peaks 447 MB |
| *Local* embeddings (bge-small in the API process), all three documents, 6 questions | all processed (NIST 52.6 s), all six answers correct | **837 MB** sampled total, API process peak 786 MB: does not fit 512 MB |

Conclusions: only hosted embeddings fit a 512 MB instance, and with them the backend stays around
320-450 MB. The binding limit is then the **embedding quota of the free Gemini key**: it is small
and not published (AI Studio shows yours), and on 2026-11-15 a few large uploads plus retries used it
up for the rest of the day. The variant therefore throttles embedding requests
(`EMBEDDING_API_INPUTS_PER_MINUTE`), caches embeddings so retries do not spend quota twice, waits on
429 answers, and after `INGESTION_MAX_ATTEMPTS` marks the document "failed, you can retry" instead of
looping. For heavier use, point `EMBEDDING_API_BASE_URL`/`EMBEDDING_API_KEY`/`EMBEDDING_MODEL` at any
other OpenAI-compatible embedding endpoint with a larger quota.

Retrieval without the re-ranker: on the real-document set with the local model, dense search ranked
the right passage first more often than RRF hybrid (hit@1 0.61 against 0.46; main's
`evaluation/reports/retrieval_real_docs.md`), so the template uses `RETRIEVAL_MODE_WITHOUT_RERANK=dense`.
The same evaluation with Gemini embeddings (`EMBEDDING_BACKEND=hosted python evaluation/retrieval/real_docs.py`)
could not run on 2026-11-15 because the quota was exhausted; run it once you have a fresh key, and
switch to `hybrid` if it measures better (BM25 vectors are indexed either way, so no re-index is needed).

## Step by step

You need: a GitHub account with this repository pushed (branch `free-tier`), and free accounts at
Supabase, Qdrant Cloud, Render, Streamlit Community Cloud and Google AI Studio. None of them asks
for a card for the plans used here (check at sign-up; terms change).

### 1. Secrets you generate once

Run anywhere with Python (or use any password generator for the first three):

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"   # JWT_SECRET
python -c "import secrets; print(secrets.token_urlsafe(48))"   # FRONTEND_CLIENT_IP_SECRET
python -c "import secrets; print(secrets.token_urlsafe(18))"   # REGISTRATION_INVITE_CODE
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"  # MFA_ENCRYPTION_KEY
```

Create a **new** Gemini API key in Google AI Studio (and delete the old, leaked one).

### 2. Supabase (database and file storage)

1. New project, region Frankfurt (next to the Render region below).
2. SQL Editor: run `deploy/supabase.sql` once. Then set a password for the `rag_app` role
   (Database > Roles, or `ALTER ROLE rag_app WITH PASSWORD '...'` in the editor).
3. Connect > **Session pooler**: copy the host. User is `rag_app.<project-ref>`, port 5432, database `postgres`.
4. Database settings: enable SSL enforcement and download the CA certificate (`supabase-ca.crt`).
5. Storage: create a **private** bucket `rag-documents`; Storage settings > S3: enable, note the
   endpoint and region, create an access key pair.
6. Keep the `rag` schema out of the API's "Exposed schemas" list.

### 3. Qdrant Cloud (vectors)

Create a free cluster (choose a region near Frankfurt), then an API key. Note the cluster URL.

### 4. Render (API)

1. Dashboard > Environment Groups > New: name it `rag-free`. Paste every line of
   `deploy/free/backend.env.example` and replace each `REPLACE...` value with yours from steps 1-3.
   Add a **secret file** named `supabase-ca.crt` with the certificate's contents.
2. New > **Blueprint**: connect the GitHub repository, branch `free-tier`, Blueprint path
   `deploy/free/render.yaml`. It creates the free web service `rag-free-api` and the free Key Value
   `rag-free-kv` (whose URL is wired in automatically).
3. Wait for the first build and start (database migrations run at start). Check:
   `https://rag-free-api.onrender.com/api/v1/health/ready` (your service name may differ)
   should answer `{"status":"ok", ...}`.

### 5. Streamlit Community Cloud (UI)

1. New app: repository, branch `free-tier`, main file `frontend/app.py`, Python 3.13 (Advanced settings).
2. Secrets: paste `deploy/free/streamlit-secrets.toml.example` with your API URL and the same
   `FRONTEND_CLIENT_IP_SECRET` as on Render.
3. Deploy. Copy the app's URL (`https://<name>.streamlit.app`).

### 6. Finish

1. In the Render group `rag-free`, set `APP_BASE_URL` to the Streamlit URL; redeploy the service.
2. GitHub repository > Settings > Variables > Actions: add `FREE_API_URL` =
   `https://<your-service>.onrender.com/api/v1` (enables the keep-alive job).
3. Open the Streamlit URL, create your account with the invite code, upload a PDF, wait for
   Ready, ask a question and open the sources. Share the URL and the invite code with your users.

## Limits to be aware of

- Render free: 750 instance hours per month per workspace (one always-awake service is ~720 h);
  when it sleeps, background processing pauses too and resumes on the next visit (the start-up
  reconcile picks up unfinished documents).
- Large uploads embed at the configured pace (`EMBEDDING_API_INPUTS_PER_MINUTE`, default 60):
  an 80-page standard (218 passages) takes a few minutes. Raise it if your AI Studio quota allows.
- Memory: the API and its parser child processes must stay under 512 MB; keep `INSPECT_POOL_SIZE`
  and `PARSE_POOL_SIZE` at 1 and do not enable Docling, OCR, the re-ranker or local embeddings here.
- Render Key Value is in memory: a restart clears rate-limit counters and the model quota counter
  (the provider still enforces its own quota).
- Qdrant suspends free clusters after a week unused, and Supabase can pause inactive free projects; the keep-alive job is there to prevent both.
- Nothing here has been deployed by the author; the first real deployment is the final test.
