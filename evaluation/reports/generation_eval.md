# Generation evaluation (real free hosted model, synthetic corpus)

Model `gemini-3.5-flash-lite`, 2026-11-11; 46 questions (36 answerable, 10 unanswerable scored). Production agent graph; persistence disabled.

| metric | value |
|---|---|
| provider_errors | {} |
| json_first_try | 0.94 |
| repaired | 0.00 |
| fallback | 0.00 |
| citation_gold | 0.94 |
| answer_contains_mean | 0.93 |
| refusal_accuracy | 1.00 |
| latency_p50_s | 3.46 |
| latency_p95_s | 6.93 |
| llm_calls_mean | 1 |

HONEST LIMITS: small synthetic set, one model, free-tier throttling (errors are counted above, not hidden).
