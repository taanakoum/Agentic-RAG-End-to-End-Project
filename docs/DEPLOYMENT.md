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

- Retrieval models (embedding + BAAI/bge-reranker-v2-m3 int8, ~0.6 GB download) are cached in the `models` volume, now mounted by api and worker. Each API process loads them (`WARM_MODELS_ON_STARTUP=true` in compose); the api memory limit was raised to 4g for `WEB_CONCURRENCY=2` (UNVERIFIED, KI-025). Search latency on CPU: ~10 ms hybrid, ~2.2 s with the default reranker (KI-023); set `RERANK_ENABLED=false` for the fast path.

## Native development (what was actually verified)
`scripts/dev_services.ps1` starts PostgreSQL, Redis and Qdrant from `C:\Users\ali_a\devtools`.
Run tests with `export UV_PROJECT_ENVIRONMENT=C:/Users/ali_a/devtools/venv` then
`uv run --no-sync pytest`. No cloud account or paid service is needed anywhere.

## Language model (free hosted API; no local model)
The platform talks to ONE OpenAI-compatible endpoint, so any of these free options is configuration only. Nothing has been created for you and no account was opened; names and free-tier limits change, so confirm on the provider's site.
| Provider | `LLM_BASE_URL` | Example `LLM_MODEL` | Notes |
|---|---|---|---|
| Google Gemini (AI Studio key) | `https://generativelanguage.googleapis.com/v1beta/openai/` | `gemini-2.5-flash` | free tier with rate limits; check the data-use terms |
| OpenRouter | `https://openrouter.ai/api/v1` | a model ending in `:free` | set `LLM_EXTRA_HEADERS={"HTTP-Referer":"http://localhost","X-Title":"agentic-rag"}` if required |
| Groq | `https://api.groq.com/openai/v1` | e.g. `llama-3.3-70b-versatile` | very fast, strict free limits |
| Hugging Face Inference Providers | `https://router.huggingface.co/v1` | a model id from the router catalogue | uses your HF token as the key |
Set in `.env` (never commit): `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`, optionally `LLM_FALLBACK_MODELS=model-a,model-b` (tried in order on 429/404/5xx), `LLM_JSON_MODE=json_object|json_schema|prompt` (use `prompt` if a provider rejects `response_format`; a 400 also downgrades automatically), `LLM_MAX_CONCURRENCY`, `LLM_CACHE_TTL_S`. Without a key, smalltalk and clarification still work and document questions return `503 MODEL_NOT_CONFIGURED`.
Verify with a real model (the test suite itself never calls one):
```
$env:REAL_LLM_BASE_URL="..."; $env:REAL_LLM_API_KEY="..."; $env:REAL_LLM_MODEL="..."
uv run --no-sync pytest backend/tests/integration/test_chat_e2e.py -m real_llm -s
$env:PYTHONPATH="backend;."; uv run --no-sync python evaluation/generation/run_eval.py --delay 4
```
The second command writes `evaluation/reports/generation_eval.md` (JSON validity, repair/fallback rates, citation validity against the gold chunk, refusal accuracy, latency, 429 count).
