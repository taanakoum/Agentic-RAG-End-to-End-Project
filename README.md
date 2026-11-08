# Agentic RAG Platform

Async, multi-user Agentic RAG platform (FastAPI, LangGraph, PostgreSQL, Qdrant, Redis/Celery, Streamlit).
Requirements: `docs/spec/`. Progress: `docs/IMPLEMENTATION_STATUS.md`. Log: `docs/EXECUTION_LOG.md`.

## Development setup (Windows, native services; Docker path is unverified, see KI-003)
```powershell
# one-time: services are installed under C:\Users\<you>\devtools (see docs/EXECUTION_LOG.md Entry 2)
$env:UV_PROJECT_ENVIRONMENT = "$env:USERPROFILE\devtools\venv"   # keep the venv outside OneDrive
uv sync                                                           # Python 3.13, pinned by uv.lock
powershell -File scripts/dev_services.ps1 start                   # PostgreSQL, Redis, Qdrant (status|stop)
uv run --no-sync alembic -c backend/alembic.ini upgrade head      # migrations (uses .env / rag_dev)
```
Copy `.env.example` to `.env` and fill in values (`.env` is gitignored).

Run the API and a worker (Windows needs `--pool=solo`; Linux/Docker uses the default prefork pool):
```powershell
cd backend
$env:PYTHONPATH = "."
uvicorn app.main:app --port 8000
celery -A app.tasks.celery_app:celery_app worker --pool=solo --loglevel=INFO
celery -A app.tasks.celery_app:celery_app beat --loglevel=INFO   # outbox dispatcher + reconciler
```
Health: `GET /api/v1/health/live`, `/ready` (PostgreSQL, Redis, Qdrant, storage), `/workers`.

## Quality commands
```powershell
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv run --no-sync pyright
uv run --no-sync pytest backend/tests/unit          # no services needed
uv run --no-sync pytest backend/tests/integration   # needs the dev services; uses rag_test, Redis db 15
uv run --no-sync pytest                             # everything
```
Tests never touch the dev database: `backend/tests/conftest.py` forces `rag_test`, Redis db 15 and a `test_` Qdrant prefix.

## Docker (UNVERIFIED)
`docker-compose.yml` and `backend/Dockerfile` are written but have never been built or run (Docker is not installed on the dev machine).
