# Known issues

Only current issues are listed. Issues that were fixed have been removed (the history is in
`docs/EXECUTION_LOG.md` and git). IDs are kept so older documents and commits still make sense.

Last review: 2026-11-12.

## 1. Needs action from you

| # | What | Why it matters | How to do it |
|---|---|---|---|
| A1 | **Rotate the Gemini API key** (and decide what to do with the old key in git history) - KI-040 | The key was pasted into a chat and, for a short time, used as a sample value in a test: it is in the **local, unpushed** commit `a555334`. Anyone who could read that commit could use the key | 1. Create a new key at <https://aistudio.google.com/apikey> and delete the old one there (this alone makes the old key worthless). 2. Put the new key in `.env` (`LLM_API_KEY=`). 3. Before publishing, either publish from a fresh history (`git checkout --orphan public-main`, `git add -A`, `git commit -m "Initial public version"`, push that branch as `main`) or rewrite history on a fresh clone with `uvx git-filter-repo --replace-text expressions.txt` (a file containing `<old key>==>REMOVED`). I did not rewrite history myself because it changes your commits |
| A2 | **Docker verification** (WSL2) - KI-003, KI-009, KI-014, KI-019, KI-020, KI-025; also the Docker smoke test of Phase 10 and gate G1 | The images and the compose stack were written but have never been built or run, so container behaviour, the Linux sandbox limits and the memory limits are unverified | In an *administrator* PowerShell: `wsl --install --no-distribution`, reboot, start Docker Desktop, then `powershell -ExecutionPolicy Bypass -File scripts/verify_g1.ps1`. Paste its output into `docs/EXECUTION_LOG.md`. Details: `docs/DEPLOYMENT.md` |
| A3 | **Choose a licence** | Without a `LICENSE` file a public repository is "all rights reserved" and nobody may legally reuse it | Add a `LICENSE` file (MIT or Apache-2.0 are the usual choices for a portfolio project) and update the License section of `README.md` |
| A4 | **Check `.env.example`** | A permission rule blocks my tooling from reading `.env.*`, so I could not review it for stale or real values before the repo goes public | Open it, confirm it only has placeholders. `python scripts/init_env.py` is the maintained way to create a `.env`; you may `git rm .env.example` and keep only that script |
| A5 | **Decide where documents may go** - KI-034 | Free hosted models receive question and document text; free tiers of some providers may log or train on prompts | Pick a provider whose terms fit the data, or self-host an OpenAI-compatible server (vLLM, llama.cpp, Ollama) and set `LLM_BASE_URL`. See `docs/SECURITY.md` |
| A6 | *(optional)* **Verify S3 storage** - KI-013 | The S3 adapter is verified only against an S3 emulator (moto), not MinIO or AWS | When you pick an S3-compatible store: run `pytest backend/tests/integration/test_storage_contract.py` with the `S3_*` variables pointing at it |

## 2. Open, no action needed from you

Accepted limitations, trade-offs and planned work. "Plan" says when it will be looked at.

### Security and abuse protection
| ID | Limitation | Plan |
|---|---|---|
| KI-012 | Rate limits are fixed windows (up to 2x bursts at a boundary) and the per-account lockout allows a deliberate lock-out of a victim; a Redis outage makes sign-in return 503 (fail closed, ADR-008) | Accepted trade-offs |
| KI-011 | Admin-forced MFA does not exist; the breached-password list is ~135k common passwords (offline), not a billions-entry corpus | Deferred |
| KI-017 | Uploaded files are parsed in sandboxed child processes with time/memory limits, but there is **no malware scanning** (ClamAV is open source and could be added); the Linux limits (`RLIMIT_DATA`) are implemented but unexercised until Docker works (A2) | Deferred; Linux part follows A2 |

### Quality of answers and retrieval
| ID | Limitation | Plan |
|---|---|---|
| KI-018 | Chunking and retrieval were evaluated on a small **synthetic** corpus (6 documents, 36 questions): the numbers rank configurations, they do not guarantee quality on other data | Re-run `evaluation/retrieval/run_eval.py` on real documents when available |
| KI-028 | The memory relevance threshold (0.60) was calibrated on a tiny English hand-written set; other languages, longer memories or another embedding model need re-calibration; the memory sensitivity filter is heuristic | Re-run `evaluation/memory/calibrate.py` whenever the embedding model changes |
| KI-026 | Long-term memory consolidation is rule-based (explicit self-statements only). Episodic memories and conversation summaries would need an LLM extractor; the `MemoryExtractor` protocol and the whole pipeline are ready for one | Deferred feature (it would spend model quota on every conversation) |
| KI-031 | The LLM cache rarely hits in multi-turn chat (history is part of the prompt); retrieval and re-rank results are not cached, so the re-ranker runs on every question | Measure, then consider caching retrieval per (user, query, knowledge-base version) |
| KI-032 | By design, an answer is streamed only after it is validated and saved, so the first token arrives together with the full answer (status events and keep-alives cover the wait) | Revisit only if a safe provisional-streaming design is found |
| KI-016 | OCR models target English/Chinese; OCR runs only for pages without a text layer | Accepted |

### Performance (Phase 11 will measure these)
| ID | Limitation | Plan |
|---|---|---|
| KI-021 | Docling costs about 1.7 s per page on a laptop CPU, plus a cold start; the first PDF after a start is slow (a 3-page PDF took 24 s to parse in a real trace). `PDF_PIPELINE=light` is the fast fallback | Measure on target hardware |
| KI-023 | The default re-ranker (bge-reranker-v2-m3, int8) costs about 0.45 s per candidate on CPU (3 s of a 5.5 s question in a real trace); concurrent searches queue on `INFERENCE_THREADS`. Smaller re-rankers were measured and were worse than no re-ranking on the evaluation data | Options: GPU, caching, `RERANK_ENABLED=false` (10 ms hybrid) |
| KI-024 | The Qdrant filter lists the caller's readable document ids; a user with thousands of documents sends a large filter on every query | Measure in Phase 11 |
| KI-022 | The only concurrency measurement so far is a single-client benchmark on a laptop (`load_testing/concurrency_bench.py`); it shows the event loop stays responsive, not capacity | Phase 11 (Locust is installed; scenarios not written yet) |
| KI-038 | Tracing overhead under load is unmeasured (it adds a little event-loop work; see KI-045) | Phase 11 |

### Language model
| ID | Limitation | Plan |
|---|---|---|
| KI-035 | Free-tier model access is volatile: daily caps per model, retired model names (404), busy models (503), "thinking" models that use the whole small `max_tokens` before answering | Keep `LLM_FALLBACK_MODELS` set, use a lite/non-thinking model, check the provider's list before a demo |
| KI-036 | The quota guard counts only this application's requests and its daily budget (150) is a conservative guess; other clients of the same key can still cause a provider 429 (the fallback chain and 429 handling then take over) | Re-calibrate `LLM_DAILY_REQUEST_BUDGET` from observed 429s |

### Platform and operations
| ID | Limitation | Plan |
|---|---|---|
| KI-027 | psycopg async (LangGraph checkpoints) needs a *selector* event loop on Windows. uvicorn creates a Proactor loop regardless of the loop policy, so the server must be started with `--loop app.chat.checkpointer:selector_event_loop` (`scripts/stack.py` does this; Linux containers are unaffected). The loop-policy call used by tests is deprecated from Python 3.14 (the project pins 3.13) | Revisit when moving to Python 3.14+ |
| KI-048 | Once, on the real stack, the first streamed answer after a start produced only keep-alives (the checkpointer's first initialisation waited, probably on its setup advisory lock held by a leftover backend of a killed process). It could not be reproduced afterwards (clean runs: 5). The initialisation is now time-bounded (30 s), logged by step (`checkpointer_init_timeout`) and retried on the next request; the root cause is not proven | If you ever see it, the API log line names the step |
| KI-008 | The Celery worker logs a harmless `NoneType ... connection_errors` message when Redis restarts (upstream noise); it reconnects and keeps working | Re-check on Celery/kombu upgrades |

### Observability
| ID | Limitation | Plan |
|---|---|---|
| KI-047 | OpenTelemetry's SQLAlchemy instrumentation does not support the installed SQLAlchemy 2.1.8 yet ("only instruments sqlalchemy < 2.1.0"), so database queries do not appear as spans (HTTP, Redis, agent, retrieval, model and worker spans do). A logged error at start-up says so | Upgrade `opentelemetry-instrumentation-sqlalchemy` when it supports 2.1 |
| KI-037 | OTLP export is verified against a local receiver and **Arize Phoenix 21** (spans arrive with their hierarchy and without private attributes); Langfuse and other collectors are not verified (they use the same standard endpoint) | Try Langfuse if you adopt it |

### Interface
| ID | Limitation | Plan |
|---|---|---|
| KI-041 | Reloading the page (or opening a second tab) signs you out: tokens are deliberately not stored in the browser | Accepted; httpOnly-cookie sessions behind a reverse proxy would be the alternative |
| KI-049 | Uploads show no byte-level progress bar (Streamlit does not expose one); the document list polls every 3 s while documents are processing | Accepted |

### Tests
| ID | Limitation | Plan |
|---|---|---|
| KI-045 | `test_event_loop_stays_responsive_under_concurrent_logins_uploads_and_reads` is a wall-clock assertion: it failed in three full-suite runs (passing alone and in the later runs) on a loaded laptop. Tolerance is "at most 2 stalls over 150 ms"; a mutation that blocks Argon2 on the loop still fails it (3-4 stalls, up to 592 ms), but the margin is thin | Consider asserting on thread-pool use instead of wall-clock stalls |
| KI-046 | Line coverage of the in-process code is **93.2%** (8,541 of 9,166 statements; `pytest --cov`, 2026-11-13): agents 98.9%, API routers 100%, services 95.9%, retrieval 98.6%, LLM 98.4%, ingestion 85.9%, tasks 81.6%, frontend views 79.4%. Code that runs in sandboxed child processes or separate worker processes (e.g. `sandbox_jobs.py`, 29%) is exercised by the tests but not measured, so the real figure is higher. `uvx pip-audit` on the locked dependencies reported no known vulnerabilities (2026-11-12) | Run `coverage` and `uvx pip-audit` again before a release |
