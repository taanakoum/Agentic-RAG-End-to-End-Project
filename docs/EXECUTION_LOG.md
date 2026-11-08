# Execution Log

Append one entry per milestone: what was implemented, files changed, decisions, tests run (exact command), ACTUAL results, defects found/resolved, remaining work, next step, usage reading.

---
## Entry 0: Project control files seeded
- Created CLAUDE.md, docs/spec/*, skills, commands, status files from the master specification, addendum, original prompt and execution directive.
- No application code exists yet. Next step: Phase 0 reconnaissance.

---
## Entry 1: Phase 0 reconnaissance
- Date: 2026-11-08. Branch: feature/phase-0-recon (new; `git init` was needed, folder was not a repo).
- Commands run and results:
  - `git status/branch/log` -> "not a git repository" -> ran `git init -b feature/phase-0-recon`.
  - `python --version` -> 3.14.3; `uv --version` -> 0.10.2; `uv python list` -> 3.13.12 managed install present.
  - `command -v docker podman wsl psql postgres redis-server qdrant tesseract pdftoppm` -> docker, podman, psql, postgres, redis-server, qdrant, tesseract, pdftoppm all missing. `wsl.exe -l -v` -> "Windows Subsystem for Linux is not installed".
  - PyPI JSON API queried for 33 planned dependencies (versions in docs/IMPLEMENTATION_PLAN.md). Network OK.
- Files created: docs/IMPLEMENTATION_PLAN.md (assessment, risk register), docs/ARCHITECTURE.md (baseline), ADR-001/002, .gitignore. Updated KNOWN_ISSUES (KI-002 confirmed, KI-003 blocker, KI-004), STATUS.
- Not done: official library docs (FastAPI, LangGraph, SQLAlchemy, Celery, Qdrant) were NOT read in this phase; only PyPI version numbers were checked. Deferred to the start of each phase that uses them.
- Phase 0 exit condition (repo understood, work preserved, plan documented): met.
- Blocker: KI-003. Phase 1 gate G1 needs Docker. I did not install Docker Desktop (admin, reboot, system change) without user approval, and did not start Phase 1 code without a way to verify it against real services.
- Usage: could not be measured.
- Next step: see IMPLEMENTATION_STATUS.md.

---
## Entry 2: Native dev services installed (user-approved option 2), start NOT verified
- Date: 2026-11-08. User approved installing native Windows Postgres, Redis-compatible and Qdrant for development only.
- Downloaded to C:\Users\ali_a\devtools\downloads and extracted to devtools\{postgres,qdrant,redis}:
  - pg.zip: postgresql-17.7-1-windows-x64-binaries.zip (EDB), SHA-256 2aacc055d9bac49763ac0164759c13866bc0235123d7ffdbb6a6aaa20dc25d9a
  - qdrant.zip: qdrant v1.20.0 qdrant-x86_64-pc-windows-msvc.zip (official GitHub release; size matches release listing), SHA-256 6d13553584fd0411f00637519e293331ff0aa1fb79bd9e30db45839bb75de2be
  - redis.zip: redis-windows/redis-windows 8.10.3 Redis-8.10.3-Windows-x64-msys2.zip (third-party, unofficial build; size matches listing), SHA-256 af16df340525b431e9ec8dabfd625d5e7a233f82805b5418819904ea909c447f
  - No upstream checksum comparison was done; SHA-256 values are recorded only.
- `initdb -D devtools\data\pg -U postgres --auth=scram-sha-256 -E UTF8 --locale=C` -> "Success". Superuser password written to gitignored `.env` (POSTGRES_SUPERUSER_PASSWORD). Ports 5432/6379/6333/6334 were free before install.
- Wrote scripts/dev_services.ps1 (start/stop/status; all services bound to 127.0.0.1; Redis AOF on).
- Attempted `scripts/dev_services.ps1 start`: the command was DENIED by the auto-mode classifier (no reason given). I did not try to work around it. Services have NOT been started, so nothing is verified yet.
- Next step: user runs `powershell -File scripts/dev_services.ps1 start` (or allows the command), then verify: `psql` login, `redis-cli ping`/PING via redis-py, `curl http://127.0.0.1:6333/healthz`, and persistence across a stop/start.

---
## Entry 3: Native dev services started and verified (dev only)
- Date: 2026-11-08. Bash permission granted by the user for `scripts/dev_services.ps1 start`.
- Defects found and fixed in scripts/dev_services.ps1: (1) `pg_ctl start | Out-Null` hung because the postgres child kept the caller's pipe open; (2) `Start-Process -Wait` on pg_ctl also hung (waits on the postgres child). Fixed by launching pg_ctl without -Wait, with redirected output, and polling port 5432. Side effect while debugging: stopping a hung task killed Postgres's console (crash 0xC0000142 in postgres.log, clean abort, no data loss); restarted normally.
- Verification (actual output):
  - Postgres: `select version()` -> "PostgreSQL 17.7 on x86_64-windows ... 64-bit" (password auth). Wrong password -> "password authentication failed for user postgres".
  - Redis: `redis-cli ping` -> PONG; `redis_version:8.10.3`, standalone.
  - Qdrant: `/healthz` -> "healthz check passed"; version 1.20.0; collection create + upsert -> ok.
  - Persistence: wrote a PG row, Redis key, Qdrant point; ran `dev_services.ps1 stop` (all 4 ports down) then `start` (all 4 UP); read back "persist-me" from PG, Redis, and Qdrant (point 1 with payload). Test data was then removed.
  - Script start/stop/status finally runs to completion (exit without hanging).
- Scope: DEV ONLY on native Windows binaries. Does NOT satisfy G1/G11/G12 (Docker still required, KI-003 open).
- Next step: Phase 1 (Foundation) using native services for dev/integration; Docker-dependent G1 items stay pending. Read docs/spec/01-architecture.md and 07-testing-eval-ops.md plus current official docs first.
