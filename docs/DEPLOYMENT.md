# Deployment

STATUS: the Compose stack and Dockerfile are written but have NEVER been built or run (KI-009,
KI-014). Nothing below has been verified in containers. Everything marked "native" was verified on
the dev machine with locally installed PostgreSQL, Redis and Qdrant.

## Prerequisites on Windows (one-time, needs an administrator and a reboot)
1. Admin PowerShell: `wsl --install --no-distribution`, then reboot. (Docker Desktop's backend needs
   WSL2; its log says `DockerDesktop/Wsl/NotInstalled` until this is done.)
2. Start Docker Desktop and wait for "Engine running".
3. In `.env` (never committed) set `COMPOSE_POSTGRES_PASSWORD`, `JWT_SECRET` (>= 32 chars) and
   `MFA_ENCRYPTION_KEY` (a Fernet key: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`).
4. From the repository root: `powershell -ExecutionPolicy Bypass -File scripts/verify_g1.ps1`.

`verify_g1.ps1` builds the images, starts postgres/redis/qdrant/migrate/api/worker/beat from clean
volumes, checks `/health/ready` and `/health/workers`, runs the Linux sandbox self-check
(`python -m app.ingestion.sandbox_selfcheck`: timeout kill, memory cap, secret-free environment,
warm-process recycling), ingests a real PDF through the containers, restarts the containers and then
recreates them, and proves the document, vectors and user survived. It stops at the first failure.
Paste its output into `docs/EXECUTION_LOG.md`; only then can G1 and spec test 13 (KI-019) be marked
VERIFIED.

## Services
| Service | Role | Scale |
|---|---|---|
| api | FastAPI under uvicorn, `WEB_CONCURRENCY` processes (default 2) | each process has its own event loop, DB pool and warm parser sandbox |
| worker | Celery ingestion worker | `CELERY_WORKER_CONCURRENCY` (default 2); memory ~ concurrency x `PARSE_MEMORY_MB` |
| beat | outbox dispatcher, reconciler, orphan sweep | exactly ONE |
| migrate | `alembic upgrade head`, runs once before api/worker/beat | |
| postgres, redis, qdrant | state | named volumes `pgdata`, `redisdata`, `qdrantdata` |

Objects, outbox files and model caches live in named volumes (`objects`, `outbox`, `models`).
An S3-compatible store can replace the local volume (see ADR-011; verified against moto only, KI-013).

## Sizing and resource notes
- The image installs CPU-only PyTorch on Linux (pytorch-cpu index) because Docling needs it; the
  image is large and the first build is slow.
- Docling cold start loads layout models (seconds); the warm sandbox pool amortises this per worker
  process. Docling CPU cost measured on the dev laptop: ~1.7 s/page for conversion
  (`evaluation/reports/chunking_eval.md`). Use `PDF_PIPELINE=light` for a much cheaper, lower
  quality parser.
- Sandbox limits on Linux use `RLIMIT_DATA`; this path is implemented but only the Windows Job
  Object path has been exercised (KI-020). The self-check above is the verification step.

## Native development (what was actually verified)
`scripts/dev_services.ps1` starts PostgreSQL, Redis and Qdrant from `C:\Users\ali_a\devtools`.
Run tests with `export UV_PROJECT_ENVIRONMENT=C:/Users/ali_a/devtools/venv` then
`uv run --no-sync pytest`. No cloud account or paid service is needed anywhere.
