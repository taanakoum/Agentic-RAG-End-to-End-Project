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
The Streamlit UI (optional extra `frontend`) is started as described in docs/FRONTEND.md.

## Language model (free hosted API; no local model)
The platform talks to ONE OpenAI-compatible endpoint, so any of these free options is configuration only. Nothing has been created for you and no account was opened; names and free-tier limits change, so confirm on the provider's site.
| Provider | `LLM_BASE_URL` | Example `LLM_MODEL` | Notes |
|---|---|---|---|
| Google Gemini (AI Studio key) | `https://generativelanguage.googleapis.com/v1beta/openai/` | `gemini-3.5-flash-lite` (VERIFIED 2026-11-11) | free tier with a daily cap per model; older names (`gemini-2.5-flash-lite`, `gemini-2.0-*`) were retired for new keys; check the data-use terms |
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

Verified against a live provider (2026-11-11, Gemini free tier, key supplied by the user and used only via environment variables): `pytest -m real_llm` passed with `gemini-2.5-flash` and `gemini-3.5-flash-lite`; `evaluation/generation/run_eval.py` on `gemini-3.5-flash-lite` gave 0 provider errors over 46 questions (see `evaluation/reports/generation_eval.md`). Rotate any key that was ever pasted into a chat or terminal log.

## Where the model settings live: `.env`
All model settings are in the project's `.env` (gitignored; never commit it): `LLM_BASE_URL`, `LLM_MODEL`, `LLM_FALLBACK_MODELS`, `LLM_API_KEY`, `LLM_JSON_MODE`, `LLM_MAX_TOKENS`, plus the quota settings below. The application, the worker and the evaluation scripts read them from there. The automated test suite deliberately blanks every `LLM_*` variable, so tests can never reach the hosted model or spend quota, even with a real key in `.env`.

## Calibrating daily usage (free tier)
What was measured on 2026-11-11 with the Gemini free tier (one key, one project):
| Model | Observed | Consequence |
|---|---|---|
| `gemini-2.5-flash` | 22 requests succeeded, then `429 quota exceeded` for the rest of the day | too small for evaluation or sharing; avoid as primary |
| `gemini-3.5-flash-lite` | 46 evaluation questions + 2 end-to-end runs + probes (about 55 requests) with 0 `429` | chosen primary |
| `gemini-flash-lite-latest` | answered a probe | chosen as fallback (quota is per model) |
What one chat turn costs: smalltalk and clarification 0 model calls; a normal document question 1 call (the evaluation measured a mean of 1.00 calls, 0 repairs); a short follow-up that needs a rewrite 2 calls; a repair adds 1. A cache hit costs 0 (KI-031: hits are rare in multi-turn chat).
The guard (`app/llm/budget.py`) counts every request that is about to reach the provider, per model, in Redis, and refuses locally before the provider does:
| Setting (in `.env`) | Value set | Meaning |
|---|---|---|
| `LLM_DAILY_REQUEST_BUDGET` | 150 | requests per model per provider day. A conservative guess below what was observed to work; NOT a verified cap. Lower it if you ever see a provider 429 before the budget is used up |
| `LLM_REQUESTS_PER_MINUTE` | 10 | per model; below typical free-tier RPM limits |
| `LLM_QUOTA_TIMEZONE` | `America/Los_Angeles` | the provider's day rolls over at midnight there (Gemini); `Retry-After` is the real time until then |
| `RL_CHAT_DAILY_LIMIT` | 60 | chat turns per user per 24 h, so one user cannot spend the shared quota |
| `LLM_MAX_CONCURRENCY` / `_PER_USER` | 3 / 2 | simultaneous model calls overall / per user |
When a model's budget is used up the next fallback model is tried; when all are used up the user gets `429 MODEL_RATE_LIMITED` with an accurate `Retry-After`. `GET /api/v1/health/llm` shows `configured`, `remaining_today` and `resets_in_s` (no key, no names). The guard counts only what THIS application sends: anything else using the same key (other tools, the provider's own console) also consumes the real quota, so keep the budget below the real cap.
Testing policy so the quota is never exhausted: automated tests use 0 calls; `pytest -m real_llm` uses 1 call; `python evaluation/generation/run_eval.py` uses about 50 calls and refuses to start if today's remaining budget is too small (use `--limit 12` for a smoke run, at most one full run per day, and it counts against the same budget). Never run the evaluation in a loop.

