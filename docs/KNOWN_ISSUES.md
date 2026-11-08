# Known Issues

Never delete entries; mark resolved with date and how.

| ID | Type | Description | Status | Resolution |
|---|---|---|---|---|
| KI-001 | Deferred | Non-PDF formats (TXT, MD, DOCX, CSV) not implemented; parser interface must allow adding them | OPEN | |
| KI-002 | Process | Weekly usage may be unreadable inside a session; checkpoint after each sub-milestone. Confirmed 2026-11-08: no tool available to read `/usage` in this session. | OPEN | |
| KI-003 | Blocker | Docker, Podman and WSL are not installed (checked 2026-11-08). Gate G1 (build images, start PG/Redis/Qdrant, persistence across restart) and real-service integration tests cannot run. Installing Docker Desktop needs admin rights, virtualization/WSL2 and a reboot, so it was not attempted without user approval. | OPEN | User action: install Docker Desktop (or approve an alternative from docs/IMPLEMENTATION_PLAN.md) |
| KI-005 | Env | Native dev services installed 2026-11-08 under C:\Users\ali_a\devtools (outside OneDrive): PostgreSQL 17.7-1 (EDB zip), Qdrant 1.20.0 (official zip), Redis-compatible 8.10.3 from the UNOFFICIAL redis-windows/redis-windows msys2 build. Checksums in EXECUTION_LOG Entry 2. Postgres data dir initialized; superuser password in gitignored .env. Started and verified 2026-11-08 (Entry 3): login, PING, healthz, persistence across stop/start. Dev only; does not satisfy G1/G11/G12. | OPEN | Use `scripts/dev_services.ps1 start|stop|status`. Keep open until Docker verifies G1 |
| KI-004 | Env | Default Python is 3.14.3; project will pin 3.13 (uv-managed 3.13.12 present) for dependency compatibility | OPEN | Decide/confirm in Phase 1 (ADR-001) |
| KI-006 | Env | `.env.example` could not be created: project permission settings deny access to `.env.*`. File is named `env.example` instead. | OPEN | Rename to `.env.example` manually (or relax the deny rule for that file) |
| KI-007 | Deferred | S3-compatible storage adapter not implemented (only local volume). Needed for hosted deploy (Phase 12). | OPEN | |
| KI-008 | Env | Celery worker logs `AttributeError: 'NoneType' object has no attribute 'connection_errors'` (kombu `ignore_errors` during Gossip restart) when Redis restarts. Worker reconnects and processes tasks afterwards (verified). Upstream noise, not fixed. | OPEN | Re-check on Celery/kombu upgrades |
| KI-009 | Env | Docker artifacts (backend/Dockerfile, docker-compose.yml) are written but NEVER built or run. Only YAML syntax was parsed. | OPEN | Verify when Docker is available (G1) |

