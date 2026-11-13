# Known issues

Only current issues are listed. Issues that were fixed have been removed (the history is in
`docs/EXECUTION_LOG.md` and git). IDs are kept so older documents and commits still make sense.

Last review: 2026-11-12.

## 1. Needs action from you

| # | What | Why it matters | How to do it |
|---|---|---|---|
| A1 | **Rotate the Gemini API key** - KI-040 | The key was pasted into a chat. The git history was cleaned on 2026-11-13 (the two commits that held it were rewritten and the old objects pruned; no commit or object in the repository contains it), but the key itself is still valid until you revoke it | Create a new key at <https://aistudio.google.com/apikey>, delete the old one there, put the new one in `.env` (`LLM_API_KEY=`), restart the stack |
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
| KI-018 | Retrieval was evaluated on a small synthetic corpus (6 documents, 36 questions) and, since 2026-11-13, on two real public documents (NIST SP 800-63B and the Transformer paper, 28 questions, `evaluation/retrieval/real_docs.py`). The real-text numbers are much lower (default hit@1 0.64 against 0.94) and rank the configurations differently (dense beats plain hybrid); both sets are small, written by one author, and use strict one-sentence ground truth, so neither promises quality on other data | Re-run both on your own documents; `real_docs.py` is the template |
| KI-028 | The memory relevance threshold (0.60) was re-checked on 2026-11-13 with a broader hand-written set (30 memories; 50 relevant queries: short, paraphrased, long, French/German/Spanish; 31 irrelevant ones incl. 12 near-misses): 0.60 recalls 86% and retrieves something for 19% of irrelevant queries (near-misses 25%); 0.55 recalls 92% but retrieves for 52%; 0.65 recalls only 56%. The data did not clearly support a change, so it stays. Weak spots: non-English queries (recall 50%, the model is English-only; top-1 25%) and long task-style queries (top-1 57%). The memory sensitivity filter is heuristic | Re-run `evaluation/memory/calibrate.py` whenever the embedding model changes; a multilingual embedding model would be the fix for other languages |
| KI-026 | Long-term memory consolidation is rule-based by default (explicit self-statements only). An optional model-based extractor exists (`MEMORY_LLM_EXTRACTION=true`, ADR-037: one call per consolidation, same sensitivity/consent/cap/retention rules, rule-based fallback), but it is verified only with a scripted model: the quality of what a real free model extracts, and the cost in quota, are unmeasured. Conversation summaries are not produced | Try it with a real model before enabling it; measure the extra quota use |
| KI-031 | The LLM cache rarely hits in multi-turn chat (history is part of the prompt). Retrieval results ARE now cached per user, question and knowledge-base version (ADR-036), so a repeated question skips the vector search and the re-ranker; a first-time question still pays for both | Measure the hit rate on real use |
| KI-032 | By design, an answer is streamed only after it is validated and saved, so the first token arrives together with the full answer (status events and keep-alives cover the wait) | Revisit only if a safe provisional-streaming design is found |
| KI-016 | OCR models target English/Chinese; OCR runs only for pages without a text layer | Accepted |

### Performance (Phase 11 will measure these)
| ID | Limitation | Plan |
|---|---|---|
| KI-021 | Docling costs about 1.7 s per page on a laptop CPU, plus a cold start; the first PDF after a start is slow (a 3-page PDF took 24 s to parse in a real trace). `PDF_PIPELINE=light` is the fast fallback | Measure on target hardware |
| KI-023 | The default re-ranker (bge-reranker-v2-m3, int8) costs ~2-4 s per question on a laptop CPU (5 candidates). Measured knobs (ADR-038): shorter inputs and smaller models lose quality, explicit ONNX thread counts are not faster; adaptive skip (default margin 0.1) skips ~10% of questions with no measured quality loss; models are warmed at start-up for local runs | GPU, or `RERANK_ENABLED=false` (10 ms hybrid) where the quality trade-off is acceptable |
| KI-024 | The Qdrant filter lists the caller's readable document ids; a user with thousands of documents sends a large filter on every query | Measure in Phase 11 |
| KI-022 | The only concurrency measurement so far is a single-client benchmark on a laptop (`load_testing/concurrency_bench.py`); it shows the event loop stays responsive, not capacity | Phase 11 (Locust is installed; scenarios not written yet) |
| KI-038 | Tracing overhead under load is unmeasured (it adds a little event-loop work) | Phase 11 |

### Language model
| ID | Limitation | Plan |
|---|---|---|
| KI-035 | Free-tier model access is volatile: daily caps per model, retired model names (404), busy models (503), "thinking" models that use the whole small `max_tokens` before answering | Keep `LLM_FALLBACK_MODELS` set, use a lite/non-thinking model, check the provider's list before a demo |
| KI-036 | The quota guard counts only this application's requests and its daily budget (150) is a conservative guess; other clients of the same key can still cause a provider 429 (the fallback chain and 429 handling then take over) | Re-calibrate `LLM_DAILY_REQUEST_BUDGET` from observed 429s |

### Platform and operations
| ID | Limitation | Plan |
|---|---|---|
| KI-027 | psycopg async (LangGraph checkpoints) needs a *selector* event loop on Windows, and uvicorn creates a Proactor loop regardless of any policy, so the server must be started with `--loop app.chat.checkpointer:selector_event_loop` (`scripts/stack.py` does this; Linux containers are unaffected). The deprecated event-loop policy is no longer used anywhere: the tests get the selector loop from pytest-asyncio's `pytest_asyncio_loop_factories` hook (`backend/tests/conftest.py`), the live test server from `asyncio.run(..., loop_factory=...)`, the app from uvicorn's `--loop`; a test fails if `set_event_loop_policy` reappears | Only the `--loop` flag remains (Windows only); it goes away if the checkpointer moves to a driver that supports the Proactor loop |
| KI-048 | Once, on the real stack, the first streamed answer after a start produced only keep-alives (the checkpointer's first initialisation waited, probably on its setup advisory lock held by a leftover backend of a killed process). It could not be reproduced afterwards (clean runs: 5). Mitigations: the API now initialises the checkpointer in a background task at start-up (the first turn no longer pays for it; a failed warm-up is logged as `checkpointer_warmup_failed` and the lazy path retries on the first request), and the initialisation is time-bounded (30 s) and logged by step (`checkpointer_init_timeout`). The root cause is not proven | If you ever see it, the API log line names the step |
| KI-008 | When Redis restarts under a running worker, Celery 5.7.0 / kombu 5.7.0 (the newest releases, so no upgrade fixes it) log `Error on restarting Gossip: AttributeError("'NoneType' object has no attribute 'connection_errors'")` with a traceback. It is harmless (the worker reconnects and runs the next task) and is now downgraded to DEBUG by one narrow logging filter (`KnownCeleryNoiseFilter`: logger `celery.bootsteps`, level ERROR, exactly that text); every other error passes | Remove the filter when an upstream release stops logging it (re-check on Celery/kombu upgrades) |

### Observability
| ID | Limitation | Plan |
|---|---|---|
| KI-037 | OTLP export is verified against a local receiver and **Arize Phoenix 21** (spans arrive with their hierarchy and without private attributes); Langfuse and other collectors are not verified (they use the same standard endpoint) | Try Langfuse if you adopt it |

### Tests
| ID | Limitation | Plan |
|---|---|---|
| KI-046 | Line coverage of the in-process code is **93.2%** (8,541 of 9,166 statements; `pytest --cov`, 2026-11-13): agents 98.9%, API routers 100%, services 95.9%, retrieval 98.6%, LLM 98.4%, ingestion 85.9%, tasks 81.6%, frontend views 79.4%. Code that runs in sandboxed child processes or separate worker processes (e.g. `sandbox_jobs.py`, 29%) is exercised by the tests but not measured, so the real figure is higher. `uvx pip-audit` on the locked dependencies reported no known vulnerabilities (2026-11-12) | Run `coverage` and `uvx pip-audit` again before a release |
