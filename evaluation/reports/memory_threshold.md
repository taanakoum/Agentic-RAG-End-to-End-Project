# Memory relevance threshold (real model, hand-written set `memory-threshold-v2`)

Model `BAAI/bge-small-en-v1.5`; 30 memories, 50 relevant and 31 irrelevant queries. Current default `MEMORY_MIN_SCORE=0.6`. Top-1 accuracy on the relevant queries (is the target memory the best match?): 0.68; by group: short 0.78, paraphrase 0.88, long 0.57, multilingual 0.25.

Cosine score of the target memory (min / median / max): short 0.55 / 0.69 / 0.80; paraphrase 0.60 / 0.64 / 0.71; long 0.54 / 0.68 / 0.75; multilingual 0.43 / 0.59 / 0.64. Best score any memory gets for an irrelevant query: unrelated 0.50 / 0.53 / 0.68; near_miss 0.49 / 0.58 / 0.68; foreign_unrelated 0.47 / 0.56 / 0.64.

## Overall

| threshold | relevant queries recalled | irrelevant queries that retrieve something |
|---|---|---|
| 0.30 | 1.00 | 1.00 |
| 0.35 | 1.00 | 1.00 |
| 0.40 | 1.00 | 1.00 |
| 0.45 | 0.98 | 1.00 |
| 0.50 | 0.98 | 0.81 |
| 0.55 | 0.92 | 0.52 |
| 0.60 | 0.86 | 0.19 |
| 0.65 | 0.56 | 0.10 |
| 0.70 | 0.34 | 0.00 |

## Recall of relevant queries by group

| threshold | short | paraphrase | long | multilingual |
|---|---|---|---|---|
| 0.30 | 1.00 | 1.00 | 1.00 | 1.00 |
| 0.35 | 1.00 | 1.00 | 1.00 | 1.00 |
| 0.40 | 1.00 | 1.00 | 1.00 | 1.00 |
| 0.45 | 1.00 | 1.00 | 1.00 | 0.88 |
| 0.50 | 1.00 | 1.00 | 1.00 | 0.88 |
| 0.55 | 1.00 | 1.00 | 0.86 | 0.62 |
| 0.60 | 0.93 | 1.00 | 0.86 | 0.50 |
| 0.65 | 0.78 | 0.38 | 0.57 | 0.00 |
| 0.70 | 0.48 | 0.12 | 0.43 | 0.00 |

## False retrieval of irrelevant queries by group

| threshold | unrelated | near_miss | foreign_unrelated |
|---|---|---|---|
| 0.30 | 1.00 | 1.00 | 1.00 |
| 0.35 | 1.00 | 1.00 | 1.00 |
| 0.40 | 1.00 | 1.00 | 1.00 |
| 0.45 | 1.00 | 1.00 | 1.00 |
| 0.50 | 0.75 | 0.92 | 0.67 |
| 0.55 | 0.38 | 0.67 | 0.67 |
| 0.60 | 0.12 | 0.25 | 0.33 |
| 0.65 | 0.06 | 0.17 | 0.00 |
| 0.70 | 0.00 | 0.00 | 0.00 |

HONEST LIMITS: hand-written by one author, debatable near-miss labels, an English embedding model on a few non-English queries; use it to choose a default, not as a benchmark.
