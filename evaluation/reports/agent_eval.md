# Agent evaluation (failure injection and pipeline latency)

Production LangGraph agent, real PostgreSQL, Redis and Qdrant. The model, embedder and cross-encoder are deterministic test doubles, so this measures OUR pipeline (validation, repair, citation verification, persistence, retrieval plumbing), not a hosted model. Real-model quality and token usage: see `generation_eval.md`.

**Scenarios passed: 10/10**

| scenario | expected | observed | result |
|---|---|---|---|
| well-formed cited answer | 1 call, 1 verified citation | calls=1, citations=1, insufficient=False, degraded=[] | PASS |
| malformed JSON then valid | one repair, then 1 citation | calls=2, citations=1, insufficient=False, degraded=[] | PASS |
| extra fields, wrong types | one repair, then 1 citation | calls=2, citations=1, insufficient=False, degraded=[] | PASS |
| citation to an unseen source label | citation dropped, repaired | calls=2, citations=1, insufficient=False, degraded=[] | PASS |
| quote that is not in the source | citation dropped, repaired | calls=2, citations=1, insufficient=False, degraded=[] | PASS |
| correct quote, wrong page | citation dropped, repaired | calls=2, citations=1, insufficient=False, degraded=[] | PASS |
| persistently invalid output | bounded: 2 calls, then safe fallback | calls=2, citations=0, insufficient=True, degraded=['unverified_answer'] | PASS |
| model repeats another user's secret | no leak, safe fallback | calls=2, citations=0, insufficient=True, degraded=['unverified_answer'] | PASS |
| model obeys an injected document instruction | injected text never reaches the answer | calls=2, citations=0, insufficient=True, degraded=['unverified_answer'] | PASS |
| provider outage | stable error raised, no answer stored | raised ModelProviderError | PASS |

## Latency of the pipeline without a model (20 cache-missing turns, one machine)

| span | n | p50 ms | p95 ms |
|---|---|---|---|
| agent.check_output | 20 | 8.0 | 11.5 |
| agent.generate | 20 | 11.9 | 18.0 |
| agent.persist | 20 | 19.2 | 26.1 |
| agent.retrieve | 20 | 36.7 | 51.5 |
| chat.turn | 20 | 158.2 | 201.3 |
| retrieval.rerank | 20 | 0.8 | 2.0 |
| retrieval.search | 20 | 24.5 | 34.4 |
| answer_ready (metadata.latency_ms) | 20 | 80.5 | 113.0 |

Limits: stub model (0 ms) and the hashing embedder, so absolute numbers are a floor for pipeline overhead on this dev machine, not production latency. Free-tier cost is 0.
