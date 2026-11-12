# Plan: the remaining known issues

Written 2026-11-13 (planning: Opus 5.5; execution: Sonnet 5.5, high effort). Scope: section 2 of
`docs/KNOWN_ISSUES.md` **except** "Language model", "Performance" and "Security and abuse
protection" (out of scope by the owner's decision). Interface issues KI-041 and KI-049 were fixed
just before this plan. Section 1 items (A1-A6) need the owner and are not part of this plan.

## Ground rules for the executor

- Read `CLAUDE.md` and this file first. Work on the current branch. One commit per work item, after
  its tests pass, ending with `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`.
- Never fabricate results. Every number you write down comes from a command you ran.
- Tests must not call a hosted model (the suite blanks `LLM_*`; keep it that way) and must use the
  real PostgreSQL/Redis/Qdrant like the existing integration tests. Do not run two test processes at
  once (they share the `rag_test` database).
- After each item: `uv run --no-sync ruff format .`, `ruff check .`, `pyright` (strict) clean, the
  item's tests pass, and a **mutation check** for its key property (break the fix, see a test fail,
  restore). Record the command and result in `docs/EXECUTION_LOG.md`.
- Environment: PowerShell/Git Bash on Windows; `export UV_PROJECT_ENVIRONMENT=C:/Users/ali_a/devtools/venv`
  (Bash) before `uv run --no-sync ...`. Services: `powershell -File scripts/dev_services.ps1 start`.
- Use the Write/Edit tools for files (shell heredocs break on quotes in this environment).
- Keep everything that works working: the full suite must pass at the end
  (`uv run --no-sync pytest backend/tests -q`, ~20-25 min).
- When an item turns out to need the owner (an account, money, Docker), stop that item, document why
  in `docs/KNOWN_ISSUES.md` section 1, and continue with the next one.

## Work items (in this order)

### 1. KI-047: database spans without the unsupported SQLAlchemy instrumentation
Problem: `opentelemetry-instrumentation-sqlalchemy` refuses SQLAlchemy 2.1.8, so no DB spans.
Do: in `backend/app/core/telemetry.py`, stop calling the library instrumentor for SQLAlchemy and add a
small privacy-safe instrumentation using SQLAlchemy engine events (`before_cursor_execute` /
`after_cursor_execute` / `handle_error`) on the sync and async engines created in
`app/database/session.py` (async: `engine.sync_engine`). Span name `db <OPERATION>` (first SQL keyword:
SELECT/INSERT/UPDATE/DELETE/other), attributes `db.system=postgresql`, `db.operation`, row count when
available; **never the statement text or parameters** (they can contain user content). Only active when
telemetry is active. Remove the start-up error log.
Tests: a chat turn and an upload produce `db SELECT`/`db INSERT` spans in the same trace; the privacy
scan in `tests/integration/test_observability.py` still passes and also asserts no SQL text in spans.
Mutation: record the statement text -> the privacy test fails.

### 2. KI-048: no first-turn stall: warm the checkpointer at start-up
Do: in the API lifespan (`app/main.py`), start a background task that calls
`infra.checkpointer.get()` once (with the existing bounded timeout); log success or failure; never
block start-up or fail it. Keep the lazy path as a fallback.
Tests: after app start-up (use the real lifespan, e.g. via `tests/live_backend.py`), the provider is
already initialised before the first chat request; a failure in warm-up (e.g. held advisory lock with a
short timeout) does not prevent start-up and the first request still works after the lock is released.
Mutation: remove the warm-up -> the "initialised before first request" test fails.

### 3. KI-045: replace the wall-clock event-loop test with a deterministic check
Do: keep `test_event_loop_stays_responsive...` but make its primary assertion deterministic: record the
thread on which Argon2 hashing/verification actually runs during the burst (wrap
`PasswordService.hash_sync`/`verify_sync` or the hasher) and assert it is never the event-loop thread;
keep a loose lag assertion only as a secondary signal (e.g. max stall < 2 s). Same idea for the sandboxed
PDF inspection call if it is synchronous on the loop.
Mutation: call `verify_sync` directly on the loop (as in log entry 15) -> the deterministic assertion fails
every time (run it 3 times).

### 4. KI-031: cache retrieval results per user, query and knowledge-base version
Do: in `app/retrieval/retriever.py` (or a thin wrapper used by `RetrievalService` and the agent), cache
the final ranked chunk ids + scores in Redis under
`retr:v1:u:<hashed user>:<sha256(normalized query + mode + top_k + rerank + kb_fingerprint)>`, where the
fingerprint is a hash of the caller's readable (document id, version) pairs (already computed for the
Qdrant filter). TTL `RETRIEVAL_CACHE_TTL_S` (default 600), size cap, 250 ms Redis timeout, **fail open**.
On a hit, re-load chunk payloads from Qdrant by id with the same owner filter (never trust cached text)
and drop ids that no longer exist. Purge with the account (`purge_user_cache` style). Metrics: hit/miss.
Tests: repeated question -> one Qdrant search + one rerank; a new document version, a deletion or a
different user -> miss; Redis down -> still answers; cached entries never cross users.
Mutation: drop the fingerprint from the key -> the "new version invalidates" test fails.

### 5. KI-026: optional LLM memory extractor
Do: implement an `LlmMemoryExtractor` for the existing `MemoryExtractor` protocol (`app/memory/extract.py`):
one model call per consolidation with a strict JSON schema (list of short third-person facts with kind
and importance), parsed with the existing structured-output helpers; facts pass the SAME sensitivity
filter, dedupe, consent and retention rules as the rule-based extractor. Behind
`MEMORY_LLM_EXTRACTION=false` (default off: it spends quota on every consolidated conversation), counted by
the quota guard. Fall back to the rule-based extractor on any model error.
Tests (scripted model, no network): facts are extracted and stored; sensitive facts are dropped; invalid
JSON falls back; disabled flag -> rule-based only; no consent -> nothing.
Mutation: skip the sensitivity filter for LLM facts -> a test fails.

### 6. KI-018: retrieval evaluation on real public documents
Do: `evaluation/datasets/real_docs_v1.json`: the source URLs and SHA-256 of two public documents
(NIST SP 800-63B, public domain; "Attention Is All You Need", arXiv) - never commit the files - plus
20-30 hand-written questions, each with the answer-bearing sentence copied from the document (the
relevance rule of `evaluation/retrieval/run_eval.py`). A download step in the script fetches the files to
`data/real_docs/` and verifies the hashes. Run dense / BM25 / hybrid / hybrid+rerank with the production
pipeline; write `evaluation/reports/retrieval_real_docs.md/json` with honest limits. Update
docs/EVALUATION.md and the README results if the numbers differ materially from the synthetic ones.
If downloading is impossible, record the blocker and stop this item.

### 7. KI-028: a broader memory-threshold calibration
Do: extend `evaluation/memory/calibrate.py`'s hand-written set (more memories, paraphrased and longer
queries, near-miss distractors, a handful of French/German/Spanish queries) and re-run it with the real
embedder. Change `MEMORY_MIN_SCORE` only if the new data clearly supports it (report recall/false-hit at each
threshold). Update `evaluation/reports/memory_threshold.md` and the KI text with the real numbers.

### 8. KI-027: get rid of the deprecated event-loop policy where possible
Do: check the CURRENT pytest-asyncio documentation for a supported way to give tests a selector loop
without `asyncio.set_event_loop_policy` (e.g. a loop factory option). If one exists, use it in
`backend/tests/conftest.py`, keep `app.chat.checkpointer.selector_event_loop` for uvicorn, and make
`use_selector_loop()` unnecessary for the app (the API already gets its loop from uvicorn's `--loop`;
Celery is sync). If no supported way exists, document precisely what blocks it and leave the code.
The full suite must stay green on Windows.

### 9. KI-008: the Celery reconnect noise
Do: check whether a newer Celery/kombu release (within the pinned major versions) removes the
`'NoneType' object has no attribute 'connection_errors'` log on Redis restart. If yes, upgrade (lockfile)
and verify by restarting Redis under a running worker. If not, add a narrowly scoped logging filter that
downgrades exactly that known-benign message to DEBUG, with a unit test.

### 10. KI-046: make coverage a first-class command
Do: add a `coverage` category to `scripts/test.ps1` (`COVERAGE_CORE=sysmon`, `--cov=backend/app
--cov=frontend`, terminal + JSON report into `data/`), document it in docs/TESTING_STRATEGY.md, and
turn KI-046 into a short note with the latest measured number (93.2% on 2026-11-13, or the new one).

### 11. Documentation and closing
- `docs/KNOWN_ISSUES.md`: remove fixed items, rewrite partially fixed ones with what remains. KI-032 stays
  "by design" (do not change the streaming contract: nothing unvalidated is ever shown). KI-016 stays
  accepted. KI-037 (Langfuse) needs an account or Docker: move it to section 1 as optional.
- `docs/ARCHITECTURE_DECISIONS.md`: one ADR for the retrieval cache and one for the LLM memory extractor;
  `docs/OBSERVABILITY.md` for DB spans; `docs/EXECUTION_LOG.md` entry with every command and result.
- Final: full suite + ruff + pyright; report the exact counts. Commit.
