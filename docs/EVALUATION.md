# Evaluation

Every report is produced by a documented command and committed under `evaluation/reports/`.
Ground truth is never invented after the fact: it is either derived mechanically from the data or
written by hand before looking at results (and then versioned). Most sets are small and synthetic
(the retrieval set on real documents is small too); reports rank configurations on THIS data and say so. Windows PowerShell commands; run
`scripts/dev_services.ps1 start` first when a command needs PostgreSQL/Redis/Qdrant.

| What | Command | Needs | Report |
|---|---|---|---|
| Chunking strategies | `$env:PYTHONPATH="backend;."; uv run --no-sync python evaluation/chunking/run_eval.py` | real models | `chunking_eval.md` |
| Retrieval: dense / sparse / hybrid / +rerank | `... python evaluation/retrieval/run_eval.py` | Qdrant, real models | `retrieval_eval.md` |
| Retrieval on REAL public documents (28 questions) | `... python evaluation/retrieval/real_docs.py --download` once, then `... python evaluation/retrieval/real_docs.py` | Qdrant, real models, network once | `retrieval_real_docs.md` |
| Memory threshold | `... python evaluation/memory/calibrate.py` | real embedder | `memory_threshold.md` |
| Routing + rewrite gate | `... python evaluation/agent/routing_eval.py` | nothing | `routing_eval.md` |
| Agent failure injection + pipeline latency | `$env:EVAL_WRITE_REPORT="1"; uv run --no-sync pytest backend/tests/integration/test_agent_eval.py -q` | PostgreSQL, Redis, Qdrant | `agent_eval.md` |
| Generation quality with a real free model | `... python evaluation/generation/run_eval.py --limit 10 --delay 6` | `.env` key; spends quota | `generation_eval.md` |

Datasets: `evaluation/chunking/corpus.py` (6 synthetic documents, 36 questions; relevance = the
chunk contains the answer-bearing sentence) and `evaluation/datasets/routing_v1.json` (hand-labelled
intended routes; rebuilt by `python evaluation/datasets/build_routing.py`; bump the version when
labels change).

Real-document set: `evaluation/datasets/real_docs_v1.json` holds the source URLs and SHA-256 of two
public documents (NIST SP 800-63B, 2017 edition; "Attention Is All You Need", arXiv v7), which are never
committed, and 28 hand-written questions, each with the answer-bearing sentence copied from the
document (extracted by the production parser); the relevance rule is the same as for the synthetic set.

## Metrics

- Retrieval: hit@1/3/5, recall@5, precision@3, MRR, nDCG@5/10 per configuration, median and p95 latency.
- Generation (real model): valid JSON without repair, citation correctness against gold chunks,
  insufficient-evidence behaviour on unanswerable questions, latency, tokens.
- Citations: every citation is verified server-side, so "unverified citation shown" is 0 by
  construction and is tested with fabricated labels, quotes, pages and another user's chunks;
  the agent eval reports what happens to each attack.
- Agent: routing precision/recall, rewrite-gate accuracy, repair/fallback bounds, provider-outage
  behaviour, per-stage latency from the same spans production exports.
- Cost: 0 on the free tier; tokens are recorded per answer (`metadata.prompt_tokens`,
  `completion_tokens`, spent tokens only, a cache hit spends none).

## Reading the latest numbers (honestly)

- Routing: 86 + 11 hand-labelled cases, 100% after one rule fix. The first run found a real miss
  ("and for 2023?" was not recognised as a follow-up); the rule was fixed, the label kept. The set
  was written by the author of the rules, so 100% means "behaves as intended", not "is accurate on
  real traffic".
- Agent eval: 10/10 failure-injection scenarios behave as specified; latency is pipeline overhead
  with a 0 ms model and a hashing embedder, a floor measured on one developer machine.
- Real documents (2026-11-13, 276 chunks, 28 questions): hybrid + re-rank hit@1 0.64 / hit@3 0.75 /
  MRR 0.72; dense alone 0.61 / 0.71 / 0.68; BM25 0.39 / 0.54 / 0.51; plain hybrid 0.46 / 0.68 / 0.61. That
  is much lower than the synthetic set (0.94 for the default), and the ranking of configurations differs:
  on the synthetic set hybrid beats dense, on real text dense beats plain hybrid (BM25 is weak on the NIST
  standard's repetitive "verifier SHALL ..." wording) and only the cross-encoder lifts hybrid above dense.
  NIST SP 800-63B is the hard one (hit@1 0.47 default, 0.40 dense), the research paper is easy (0.85).
  Limits: two documents, one author, strict one-sentence ground truth (a rule stated twice in the NIST
  text counts as a miss when the other place is retrieved), one machine; one question = 3.6 points.
- Generation: see `generation_eval.md` (46 questions, `gemini-3.5-flash-lite`).

## Daily quota

The real-model evaluation shares the application's quota counter (`docs/DEPLOYMENT.md`,
"Calibrating daily usage"): use `--limit` for smoke runs and check `/health/llm` first. The test
suite never calls a hosted model.
