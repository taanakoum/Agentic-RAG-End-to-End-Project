---
name: rag-phase-runner
description: Execute the next unverified phase of the Agentic RAG project end to end (define, implement, test, review, integrate, verify, document, commit). Use when starting or resuming a phase, or when asked to continue the build.
---

# Phase runner

1. Read `docs/IMPLEMENTATION_STATUS.md`, tail of `docs/EXECUTION_LOG.md`, `docs/KNOWN_ISSUES.md`. Check `git status`/branch; never discard user changes. Use a feature branch.
2. Check weekly usage. If >= 90%, do not start; run `/checkpoint` and stop.
3. Pick the first phase not VERIFIED from `docs/spec/09-phases-and-gates.md`. Read its spec file(s) in `docs/spec/`.
4. Create tasks (TaskCreate) for the phase; last task is verification.
5. For each component: define responsibility -> implement -> write tests -> run them -> review for defects -> integrate -> run integration check -> update status docs -> commit.
6. Keep the app importable/runnable after each commit. Do not build on unverified components.
7. Before docs for a library are used, check current official docs; do not copy old tutorials. If network/package install is blocked, record in KNOWN_ISSUES.md and do what is possible.
8. Run the `rag-verification-gate` skill for the phase gate. Only then mark VERIFIED with the command and result in the log.
9. Record decisions in `docs/ARCHITECTURE_DECISIONS.md`. Emit a concise progress report (phase, built, tests run + results, problems, remaining, next step).
10. Re-check usage; continue to the next phase only if < 90%.

Rules: no placeholders for required features; mocks only in tests; no fabricated results; no external accounts/spend/deploy without user authorization.
