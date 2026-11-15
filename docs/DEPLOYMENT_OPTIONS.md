# Deployment options: full feature (main) and reduced free tier (branch `free-tier`)

Last checked: 2026-11-15. Provider limits below were read from the providers' own pages on that
date (links inline); they change without notice, so re-check before you rely on them.

## The short answer

| | **Full feature** (`main`) | **Reduced, free hosting** (`free-tier` branch) |
|---|---|---|
| Where it runs | One machine you control: a free cloud VM or your own computer | Free managed services, no machine of your own |
| Card at sign-up | Oracle Cloud asks for one (identity check); your own computer: no | No card needed for the services used (see each provider) |
| Always on | VM: yes (see Oracle's idle rule); own computer: while it is on | No: the backend sleeps after 15 min idle, the UI after 12 h |
| PDF parsing | Docling layout + tables, OCR for scanned pages | Text-layer PDFs (pypdf), DOCX/TXT/MD/CSV; no OCR, no table layout |
| Retrieval | Local dense + BM25 hybrid, cross-encoder re-ranking | Hosted dense embeddings (+ BM25 if it measures better), no re-ranker |
| Background work | Separate Celery worker + beat (outbox, retries, sweeps) | Same outbox and idempotent tasks, run by a thread inside the API |
| Email | SMTP: verification, password reset | None (free hosts block SMTP): sign-up by invite code instead |
| Verified status | Native stack fully tested; Docker images written but never built | See the branch's own docs and results |

**Why two variants.** Every feature of the full system fits on one machine, and a free one exists
(Oracle's Always Free Arm VM). What does *not* exist is a free *managed* platform that runs it:
the measured footprint after one document and one question is about **436 MB for the API and
816 MB for the worker** (peak working set, light pipeline, re-ranker off, laptop measurement on
2026-11-15), while [Render's free web service](https://render.com/docs/free) is a single 512 MB
instance with no background workers and blocked SMTP ports. The free branch therefore removes
the heavy parts (local models, Docling, OCR, re-ranker, separate worker) instead of pretending they
fit.

## A. Full feature on one machine (main)

Everything runs with `docker compose` on one host, and only an HTTPS proxy is public:

```
internet ──443──▶ Caddy ──▶ Streamlit ──▶ FastAPI ──▶ PostgreSQL · Redis · Qdrant
                                              └──▶ Celery worker + beat (same machine)
```

```bash
cp .env.example .env    # fill COMPOSE_POSTGRES_PASSWORD, JWT_SECRET, MFA_ENCRYPTION_KEY, plus:
                        # SITE_DOMAIN, FRONTEND_CLIENT_IP_SECRET, EMAIL_FROM, SMTP_*, LLM_*
docker compose -f docker-compose.yml -f deploy/compose.single-box.yml up -d --build
```

What the single-box override does (`deploy/compose.single-box.yml`): production settings for every
backend service; the API's port is not published (only Caddy on 80/443 is reachable); Streamlit
calls the API on the private Docker network; Caddy obtains a Let's Encrypt certificate for
`SITE_DOMAIN` and is the only component that sees the internet. PostgreSQL stays on the private
network without TLS through an explicit, tested opt-in (`POSTGRES_PLAINTEXT_PRIVATE_NETWORK=true`,
accepted only for private addresses and service names, ADR-041).

**Status: written, never built.** There is no Docker on the development machine (KNOWN_ISSUES A2).
`backend/tests/unit/test_deploy_templates.py` checks that the compose files produce settings the
application accepts, and every native dependency in `uv.lock` has a Linux aarch64 wheel (checked
2026-11-15), but the first `docker compose up` is still the real test.

### A1. A free cloud VM (Oracle Cloud Always Free)

[Oracle's Always Free resources](https://docs.oracle.com/en-us/iaas/Content/FreeTier/resourceref.htm):
Ampere A1 (Arm) up to **2 OCPUs and 12 GB of memory**, 200 GB of block storage, 10 TB outbound per
month. Two cautions from the same page: idle Always Free instances **may be reclaimed** (95th
percentile CPU, network and memory all under 20 % over 7 days), and the free allowance was halved
in 2026, so capacity and terms move. Sign-up asks for a payment card for verification.

Sizing for 12 GB: set `CELERY_WORKER_CONCURRENCY=1`, `PARSE_MEMORY_MB=3072` and `WEB_CONCURRENCY=1`
in `.env`; open ports 80/443 in the VCN security list; point a DNS name at the VM (any domain, or a
free dynamic-DNS name) and put it in `SITE_DOMAIN`. Outbound SMTP on 587 works for a mail provider
(use a provider's SMTP relay with a verified sender).

### A2. Your own computer behind a tunnel (no card, no open ports)

Run the stack natively (`python scripts/stack.py start`, see LOCAL_SETUP.md) and publish **only the
Streamlit port** through a tunnel such as
[Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/)
(`cloudflared` makes an outbound connection, so no inbound port is opened). The API, databases and
workers stay on localhost. The site is up only while the computer is on. Set the same
`FRONTEND_CLIENT_IP_SECRET` on the API and the frontend so per-IP limits see visitors instead of
the tunnel. Check Cloudflare's current terms for accounts and hostnames before relying on it.

### A3. Paid managed hosting (reference)

`deploy/render.yaml` + `docs/HOSTING.md`: Render API/worker/beat on paid plans with Supabase
PostgreSQL and Storage. Kept as a reference; it costs money and has never been applied.

## B. Reduced variant on free managed services (branch `free-tier`)

```
Streamlit Community Cloud ──▶ Render free web service (API + in-process background tasks)
                                   ├──▶ Supabase PostgreSQL (+ Storage for uploads)
                                   ├──▶ Render Key Value (rate limits, caches; ephemeral by design)
                                   ├──▶ Qdrant Cloud free cluster
                                   └──▶ Gemini API (answers and embeddings)
```

Check out the branch and read its `docs/FREE_DEPLOYMENT.md` for the exact steps, the measured
memory, the retrieval quality of that configuration and the security trade-offs. In short, it
keeps the product (accounts, uploads with live progress, cited and server-verified answers,
conversations, memory, account controls) and gives up the parts that need RAM or SMTP.

## Common to every option

- The end user's address for per-IP limits: the frontend forwards it with the shared
  `FRONTEND_CLIENT_IP_SECRET`; without it every request appears to come from the frontend server
  and one visitor could exhaust a limit for everyone.
- The language model is an external service either way; document text reaches it (KNOWN_ISSUES A5).
- Nothing here has been deployed. No accounts were created and no deployment URL exists.
