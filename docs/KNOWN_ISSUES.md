# Known Issues

Never delete entries; mark resolved with date and how.

| ID | Type | Description | Status | Resolution |
|---|---|---|---|---|
| KI-001 | Deferred | Non-PDF formats (TXT, MD, DOCX, CSV) not implemented; parser interface must allow adding them | OPEN | |
| KI-002 | Process | Weekly usage may be unreadable inside a session; checkpoint after each sub-milestone. Confirmed 2026-11-08: no tool available to read `/usage` in this session. | OPEN | |
| KI-003 | Blocker | Docker, Podman and WSL are not installed (checked 2026-11-08). Gate G1 (build images, start PG/Redis/Qdrant, persistence across restart) and real-service integration tests cannot run. Installing Docker Desktop needs admin rights, virtualization/WSL2 and a reboot, so it was not attempted without user approval. | OPEN | User action: install Docker Desktop (or approve an alternative from docs/IMPLEMENTATION_PLAN.md) |
| KI-004 | Env | Default Python is 3.14.3; project will pin 3.13 (uv-managed 3.13.12 present) for dependency compatibility | OPEN | Decide/confirm in Phase 1 (ADR-001) |
