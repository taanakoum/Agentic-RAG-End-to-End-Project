# Run it locally: setup and first use

This guide takes you from a clean machine to a running application you can use and inspect.
Everything runs on localhost; nothing is deployed and no account is needed except an optional free
language-model API key.

## 1. What you need

| Need | Notes |
|---|---|
| Git and [uv](https://docs.astral.sh/uv/getting-started/installation/) | uv installs the pinned Python 3.13 for you |
| PostgreSQL 17, Redis, Qdrant on localhost | easiest with Docker (below); Windows users can also install them natively (last section) |
| ~6 GB free disk, ~4 GB free RAM | the search models and the Docling layout models are downloaded on first use into `data/models` |
| A free LLM API key (optional) | needed only to *answer* questions; get one in a minute from Gemini, OpenRouter or Groq |

Tested on Windows 11 with native services. The Docker commands below use the project's compose file
and have **not** been run by the author (the author's machine could not run Docker); they are plain
compose and should work, but report any problem.

## 2. Install the dependencies

```bash
git clone <this repository> agentic-rag && cd agentic-rag
uv sync --extra frontend          # creates .venv with everything pinned in uv.lock (incl. Streamlit)
```

On a synced folder (OneDrive, Dropbox) keep the virtual environment outside it:
`export UV_PROJECT_ENVIRONMENT=$HOME/devtools/venv` (PowerShell: `$env:UV_PROJECT_ENVIRONMENT = "$env:USERPROFILE/devtools/venv"`)
before `uv sync`, and in every shell you use for this project.

Every command below is shown as `uv run --no-sync <command>`; `--no-sync` stops uv from changing the
environment you just created.

## 3. Create your configuration

```bash
uv run --no-sync python scripts/init_env.py            # writes .env with fresh random secrets
```

`.env` is gitignored. It contains the JWT signing secret, the encryption key for MFA secrets, the
database password and the model settings. Open it and, if you have a key, set `LLM_API_KEY` (the file
explains where to get one). Run `init_env.py --llm openrouter` or `--llm groq` to pre-fill another provider.

## 4. Start PostgreSQL, Redis and Qdrant

**With Docker** (recommended):

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d postgres redis qdrant
```

This publishes the three services on 127.0.0.1 (5432, 6379, 6333) with data in Docker volumes.
Stop them with `docker compose ... down` (add `-v` to also delete their data).

**Without Docker on Windows**: see [the last section](#windows-without-docker).

## 5. Start the application

```bash
uv run --no-sync python scripts/stack.py start
```

This applies the database migrations and starts four processes: the API (port 8000), a Celery
worker (ingestion and memory jobs), Celery beat (outbox dispatcher, reconciler, sweepers) and the
Streamlit UI (port 8501), then waits until each one is healthy. The first start loads the search
models and takes about a minute. Useful companions:

```bash
uv run --no-sync python scripts/stack.py status
uv run --no-sync python scripts/stack.py logs api        # api | worker | beat | ui
uv run --no-sync python scripts/stack.py stop
```

Logs are in `data/logs/`, uploaded files in `data/objects/`, model caches in `data/models/`
(all gitignored).

## 6. Use it

Open **http://127.0.0.1:8501**.

1. *Create account* -> you are signed in.
2. **Documents** -> upload a PDF/DOCX/TXT/MD/CSV. The first PDF is slower (Docling downloads its
   layout models once). The status goes *Queued -> ... -> Ready*.
3. **Chat** -> ask something your document answers. Open **Sources** for the verified citations.
   Ask something it does not contain: the assistant says so.
4. Try **Memory** (switch it on, tell the assistant "I prefer short answers", ask again in a new
   conversation), **thumbs up/down**, renaming/deleting a conversation, and the **Account** page.

Emails (verification, password reset) are written as files to `data/outbox/*.eml` in development;
open them with any text editor to copy the link or token. Set `EMAIL_BACKEND=smtp` and the `SMTP_*`
variables to send real mail.

Interactive API documentation: **http://127.0.0.1:8000/docs** (development only). Health:
`/api/v1/health/live`, `/ready`, `/workers`, `/llm` (model chain and today's quota use).

## 7. See what is happening inside

- **Logs**: `python scripts/stack.py logs <name>`; every line carries `request_id`, and `trace_id`
  when tracing is on.
- **Traces**: add to `.env`: `OTEL_ENABLED=true` and `OTEL_TRACES_FILE=data/logs/traces.jsonl`, restart,
  use the app, then `uv run --no-sync python scripts/trace_viewer.py --last 3` prints each request as a tree
  with timings (API -> agent steps -> retrieval -> model call -> background worker). Details and
  Phoenix/Langfuse/collector options: [OBSERVABILITY.md](OBSERVABILITY.md).
- **Response headers**: every API response has `X-Request-ID` and `X-Trace-Id`; quote either to find
  the matching log lines and spans.

## 8. Run the tests

```bash
uv run --no-sync pytest backend/tests/unit        # ~1 minute, needs no services
uv run --no-sync pytest                           # everything (needs the three services)
```

The tests use their own database (`rag_test`), Redis database 15 and a `test_` Qdrant prefix, so they
never touch your data. Create the test database once (see *Create the databases* below).

## 9. Reset or clean up

```bash
uv run --no-sync python scripts/stack.py stop
docker compose -f docker-compose.yml -f docker-compose.dev.yml down -v     # deletes DB, Redis, Qdrant data
rm -rf data/objects data/outbox data/logs                                   # uploads, emails, logs (keep data/models)
```

## 10. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `stack.py start`: "These services are not reachable" | PostgreSQL/Redis/Qdrant are not running: step 4 |
| Migration fails with "password authentication failed" | `.env` password differs from the database's. With Docker: `docker compose ... down -v`, then up again (the volume keeps the old password) |
| "No language model is configured" in the chat | `LLM_BASE_URL`, `LLM_MODEL` and `LLM_API_KEY` are not all set; restart the stack after editing `.env` |
| "The language model is busy or its quota is used up" | free-tier limit; wait, lower `LLM_REQUESTS_PER_MINUTE`, or add `LLM_FALLBACK_MODELS`; `/api/v1/health/llm` shows today's use |
| Documents stay *Queued* | the worker or beat is not running: `stack.py status`, `stack.py logs worker` |
| First upload takes minutes | Docling is downloading its models (once). Set `PDF_PIPELINE=light` for a fast, simpler parser |
| Port 8000/8501 already in use | `stack.py --api-port 8010 --ui-port 8511 start` |
| UI asks you to sign in again after reload | by design: tokens are not stored in the browser |
| Windows: Celery errors about `fork`/prefork | the runner already uses `--pool=solo` on Windows |

## Create the databases

With the Docker services the application database `rag` already exists (the role is `rag_app`, the
password is in `.env`). Create the test database once:

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml exec postgres createdb -U rag_app rag_test
```

## Windows without Docker

The author's verified setup. Download the portable builds into `%USERPROFILE%\devtools` (or set
`DEVTOOLS_DIR`) with this layout, which `scripts/dev_services.ps1` expects:

```
devtools\postgres\pgsql\bin\      PostgreSQL 17 (EDB zip)
devtools\data\pg\                 created once with initdb
devtools\redis\...\redis-server.exe   a Redis-compatible server for Windows
devtools\qdrant\qdrant.exe        Qdrant release for Windows
```

Then, with PostgreSQL running, create the role and databases once (adjust the password to the one in
`.env`):

```powershell
psql -U postgres -c "CREATE ROLE rag_app LOGIN PASSWORD '<POSTGRES_PASSWORD from .env>'"
psql -U postgres -c "CREATE DATABASE rag OWNER rag_app"
psql -U postgres -c "CREATE DATABASE rag_test OWNER rag_app"
powershell -File scripts/dev_services.ps1 start      # status | stop
```

and continue at step 5.
