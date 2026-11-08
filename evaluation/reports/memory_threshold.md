# Memory relevance threshold (real model, hand-written set)

Model `BAAI/bge-small-en-v1.5`; 12 memories, 17 relevant and 16 irrelevant queries. Top-1 accuracy on the relevant queries: 0.82.

Cosine score of the target memory (relevant queries): min 0.56, median 0.67, max 0.78. Best score any memory gets for an irrelevant query: min 0.45, median 0.51, max 0.56.

| threshold | relevant queries recalled | irrelevant queries that retrieve something |
|---|---|---|
| 0.30 | 1.00 | 1.00 |
| 0.35 | 1.00 | 1.00 |
| 0.40 | 1.00 | 1.00 |
| 0.45 | 1.00 | 1.00 |
| 0.50 | 1.00 | 0.56 |
| 0.55 | 1.00 | 0.06 |
| 0.60 | 0.94 | 0.00 |
| 0.65 | 0.76 | 0.00 |
| 0.70 | 0.41 | 0.00 |

HONEST LIMITS: tiny hand-written English set; use it to choose a default, not as a benchmark.
