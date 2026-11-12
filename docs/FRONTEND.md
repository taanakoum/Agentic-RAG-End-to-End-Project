# Frontend (Streamlit)

`frontend/` is a Streamlit app that talks ONLY to the FastAPI backend. It has no database, vector
store or model access; the backend decides everything that matters (ownership, validation, limits,
citations). ADR-031 records the design.

## Run it (native)

```powershell
scripts\dev_services.ps1 start                       # PostgreSQL, Redis, Qdrant
$env:UV_PROJECT_ENVIRONMENT = "$env:USERPROFILE/devtools/venv"
uv sync --extra frontend                              # Streamlit is an optional extra (pinned)
uv run --no-sync alembic -c backend/alembic.ini upgrade head
uv run --no-sync uvicorn app.main:create_app --factory --app-dir backend --port 8000
uv run --no-sync celery -A app.tasks.celery_app worker -P solo -l info     # for uploads (own shell)
$env:FRONTEND_API_URL = "http://127.0.0.1:8000/api/v1"   # the default
uv run --no-sync streamlit run frontend/app.py
```

The model comes from `.env` (see docs/DEPLOYMENT.md); without it the app still works but questions
about documents answer with "No language model is configured".

## What it does

| Area | Behaviour |
|---|---|
| Sign in / register | Separate messages for missing fields, wrong credentials, taken email, weak password (field-level); second factor step when the account has MFA; expiry returns to sign-in with a reason |
| Chat | New/open/rename/delete conversations (history from the server); streamed answer with status stages; sources panel (document, page, excerpt); notices for unverified, insufficient or degraded answers; follow-up questions as buttons; thumbs up/down + comment; per-conversation options limited to top-k and re-ranking |
| Documents | Upload (PDF, DOCX, TXT, MD, CSV; the server verifies the real type), live status while processing, failure reason in plain words, Retry only for recoverable failures, Re-index, Delete (confirmed), duplicate notice |
| Memory | Opt-in switch and retention, list, add, edit, delete, reset (confirmed) |
| Account | Signed-in devices (revoke), change password, sign out |

Errors are shown by kind (credentials, document, limit, outage, conflict) with the backend's request
id as a support reference. Retry buttons appear only where repeating is safe: after an interrupted
connection the SAME idempotency key is reused (the backend replays the finished answer, the model is
not asked twice); after a failed turn the retry is a NEW request, because the backend refuses to
replay a failed turn.

## Security model

- Tokens live only in `st.session_state` (server memory of that Streamlit session): never in the
  URL, cookies, logs or browser storage. Consequence: reloading the page starts a new session and
  the person signs in again (KI-041).
- One transparent refresh on 401; a failed refresh ends the session. Sign-out revokes the refresh
  token on the server and wipes every per-user key from the session state.
- Client state is never trusted: forged `active_session` or `user` values change nothing, because
  every call carries the person's own token and the backend answers 404 for anything that is not
  theirs (tested).
- Use HTTPS in front of both the API and Streamlit when deployed.

## Tests (what was run, see the log)

- `tests/unit/test_frontend_pure.py`: error wording, retry rules, formatting.
- `tests/integration/test_frontend_api.py`: the client against the REAL backend on a real socket
  (refresh, expiry, logout revocation, SSE stream, replay, typed errors, feedback, memory,
  cross-user isolation).
- `tests/integration/test_frontend_ui.py` and `test_frontend_flows.py`: the real script run by
  Streamlit's `AppTest` against that backend and a real Celery worker: sign-in/register/expiry,
  upload, streamed answer + citations + persistence + reopening, feedback, retry semantics,
  rename/delete, documents with failures, memory CRUD, tampered state, sign-out wipe.
- Only the language model is scripted. No real browser was used (KI-042).
