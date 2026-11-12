# Frontend (Streamlit)

`frontend/` is a Streamlit app that talks ONLY to the FastAPI backend. It has no database, vector
store or model access; the backend decides everything that matters (ownership, validation, limits,
citations). ADR-031 records the design.

## Run it

Everything (migrations, API, worker, beat, UI) with one command; see [LOCAL_SETUP.md](LOCAL_SETUP.md):

```bash
uv run --no-sync python scripts/stack.py start        # UI on http://127.0.0.1:8501
```

By hand (three terminals; `FRONTEND_API_URL` defaults to `http://127.0.0.1:8000/api/v1`):

```bash
uv run --no-sync uvicorn app.main:app --app-dir backend --port 8000     # Windows: add
#   --loop app.chat.checkpointer:selector_event_loop   (psycopg async cannot use uvicorn's Proactor loop)
uv run --no-sync celery -A app.tasks.celery_app:celery_app worker --pool=solo -l info   # pool=solo on Windows
uv run --no-sync streamlit run frontend/app.py
```

The model comes from `.env`; without it the app works but questions about documents answer with
"No language model is configured".

## What it does

| Area | Behaviour |
|---|---|
| Sign in / register | Separate messages for missing fields, wrong credentials, taken email, weak password (field-level); second-factor step when the account has MFA; "Forgot your password?" (reset code by e-mail); expiry returns to sign-in with a reason |
| Chat | New/open/rename/delete conversations (history from the server); streamed answer with status stages; sources panel (document, page, excerpt); notices for unverified, insufficient or degraded answers; follow-up questions as buttons; thumbs up/down + comment (earlier ratings are remembered); per-conversation options limited to top-k and re-ranking |
| Documents | Upload (PDF, DOCX, TXT, MD, CSV; the server verifies the real type), live status while processing, failure reason in plain words, Retry only for recoverable failures, Re-index, Delete (confirmed), duplicate notice |
| Memory | Opt-in switch and retention, list, add, edit, delete, reset (confirmed) |
| Account | Verify e-mail, two-factor authentication (QR code + secret, recovery codes shown once, turn off), signed-in devices (revoke), change password, delete account, sign out |

Errors are shown by kind (credentials, document, limit, outage, conflict) with the backend's request
id as a support reference. Retry buttons appear only where repeating is safe: after an interrupted
connection the SAME idempotency key is reused (the backend replays the finished answer, the model is
not asked twice); after a failed turn the retry is a NEW request, because the backend refuses to
replay a failed turn. A 401 only ends the session when it is about the access token (a wrong
second-factor code is also a 401 and must not sign you out).

## Security model

- Tokens live only in `st.session_state` (server memory of that Streamlit session): never in the
  URL, cookies, logs or browser storage. Consequence: reloading the page starts a new session and
  the person signs in again (KI-041).
- One transparent refresh on a token 401; a failed refresh ends the session. Sign-out revokes the
  refresh token on the server and wipes every per-user key from the session state.
- Client state is never trusted: forged `active_session` or `user` values change nothing, because
  every call carries the person's own token and the backend answers 404 for anything that is not
  theirs (tested).
- Use HTTPS in front of both the API and Streamlit when deployed.

## Tests and verification

- `tests/unit/test_frontend_pure.py`: error wording, retry rules, formatting.
- `tests/integration/test_frontend_api.py`: the client against the REAL backend on a real socket
  (refresh, expiry, logout revocation, SSE stream, replay, typed errors, feedback, memory,
  cross-user isolation).
- `tests/integration/test_frontend_ui.py`, `test_frontend_flows.py`, `test_frontend_account.py`: the
  real script run by Streamlit's `AppTest` against that backend and a real Celery worker: sign-in,
  register, expiry, upload, streamed answer with citations, feedback, retry semantics, memory, MFA
  setup and sign-in, e-mail verification, password reset, account deletion, tampered state.
- **A real browser**: `scripts/browser_tour.py` drives the running app in headless Chromium
  (Playwright, installed in a throw-away environment) through sign-up, four uploads, a real model
  answer with a verified citation, memory and account pages, and saves the screenshots in
  `docs/images/`. Running it found three problems the automated tests could not (document status
  polling that never started, an upload notice wiped by a re-run, and, on the real server, the
  Windows event-loop problem above).
- Only the language model is scripted in the automated tests; the browser tour used a real free model.
