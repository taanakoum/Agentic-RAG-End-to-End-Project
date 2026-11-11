---
name: rag-verification-gate
description: Verify a quality gate (G1-G12) or phase exit condition with real commands before marking anything VERIFIED. Use before updating status to VERIFIED, before advancing phases, or when asked to verify/audit.
---

# Verification gate

Honesty first: a gate passes only on actual evidence from this project. Never fabricate or soften results.

1. Identify the gate and its criteria in `docs/spec/09-phases-and-gates.md` and `docs/spec/08-acceptance.md`.
2. Run the real checks, adapting to project config:
   - `ruff check .` ; `ruff format --check .` ; `pyright` (or mypy) ; `pytest <relevant markers/paths>`
   - Alembic upgrade on a clean DB (and downgrade where defined)
   - `docker compose up --build -d`, health/ready endpoints, restart containers and confirm data persists
   - Gate-specific scenarios (see below)
3. Record in `docs/EXECUTION_LOG.md`: exact commands, pass/fail counts, failures. Failures stay recorded.
4. If anything fails: fix the root cause (no blanket `type: ignore`, skipped tests, or weakened assertions) and re-run, or mark BLOCKED with the exact blocker in KNOWN_ISSUES.md.
5. Only after a clean pass set the status to VERIFIED with an evidence reference.

Gate-specific minimums:
- G1 services start, health OK, migrations, restart persistence.
- G2 auth tests incl. concurrent duplicate registration, refresh rotation/reuse, expiry, no hash leakage, ownership.
- G3 the 13 ingestion scenarios, outbox recovery, deletion across PG/Qdrant/storage.
- G4 Recall/Precision/MRR/NDCG for dense, sparse, hybrid, reranked on a documented dataset + manual inspection.
- G5 restart persistence, same-thread conflict behavior, memory deletion removes vector, isolation.
- G6 real E2E (scripted LLM in tests, plus a live free model run); citations verified; schema valid; streaming order. G7 (multi-agent) was removed by the user.
- G8 frontend-to-backend flows, session expiry, error presentation.
- G9 full suites + security tests. G10 concurrency + load numbers with hardware noted. G11 clean Docker + persistence. G12 docs, final report, AUDIT_REPORT.md.
