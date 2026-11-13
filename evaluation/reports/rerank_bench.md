# Re-ranking benchmark (KI-023)

Date 2026-11-13; Intel64 Family 6 Model 140 Stepping 1, GenuineIntel, Windows, CPU only; {'fastembed': '0.9.1', 'onnxruntime': '1.31.1', 'numpy': '2.5.4'}. Questions: synthetic 36 (natural style), real documents 28. Candidates come from the production hybrid retriever (top-8 fused); latency is `score()` time on 8 questions x 3 repeats after one discarded warm-up call.

HONEST LIMITS: small question sets (one question = 2.8 points on the synthetic set, 3.6 on the real one); one laptop CPU with nothing else running; latency excludes queueing and the vector search; the quality of the real set depends on one author's questions.

## Quality (hit@1 | hit@3 | MRR), every knob replayed on the same candidates

| model | chars | candidates | synthetic hit@1 | hit@3 | MRR | real hit@1 | hit@3 | MRR |
|---|---|---|---|---|---|---|---|---|
| (no rerank, fused order) | - | - | 25/36 | 32/36 | 0.801 | 14/28 | 19/28 | 0.619 |
| BAAI/bge-reranker-v2-m3-int8 | 2000 | 5 | 34/36 | 34/36 | 0.944 | 18/28 | 21/28 | 0.720 |
| BAAI/bge-reranker-v2-m3-int8 | 2000 | 8 | 34/36 | 34/36 | 0.944 | 18/28 | 22/28 | 0.738 |
| BAAI/bge-reranker-v2-m3-int8 | 1200 | 5 | 12/36 | 26/36 | 0.572 | 20/28 | 21/28 | 0.756 |
| BAAI/bge-reranker-v2-m3-int8 | 1200 | 8 | 12/36 | 26/36 | 0.555 | 21/28 | 22/28 | 0.792 |
| BAAI/bge-reranker-v2-m3-int8 | 800 | 5 | 11/36 | 28/36 | 0.561 | 19/28 | 20/28 | 0.723 |
| BAAI/bge-reranker-v2-m3-int8 | 800 | 8 | 10/36 | 26/36 | 0.529 | 19/28 | 20/28 | 0.741 |
| jinaai/jina-reranker-v2-base-multilingual | 2000 | 5 | 28/36 | 32/36 | 0.844 | 16/28 | 20/28 | 0.670 |
| jinaai/jina-reranker-v2-base-multilingual | 2000 | 8 | 28/36 | 31/36 | 0.833 | 17/28 | 20/28 | 0.684 |
| jinaai/jina-reranker-v2-base-multilingual | 1200 | 5 | 18/36 | 30/36 | 0.673 | 17/28 | 21/28 | 0.696 |
| jinaai/jina-reranker-v2-base-multilingual | 1200 | 8 | 17/36 | 28/36 | 0.635 | 18/28 | 21/28 | 0.719 |
| jinaai/jina-reranker-v2-base-multilingual | 800 | 5 | 16/36 | 27/36 | 0.631 | 16/28 | 19/28 | 0.659 |
| jinaai/jina-reranker-v2-base-multilingual | 800 | 8 | 15/36 | 25/36 | 0.590 | 17/28 | 19/28 | 0.684 |
| Xenova/ms-marco-MiniLM-L-12-v2 | 2000 | 5 | 30/36 | 33/36 | 0.876 | 20/28 | 22/28 | 0.759 |
| Xenova/ms-marco-MiniLM-L-12-v2 | 2000 | 8 | 30/36 | 33/36 | 0.874 | 20/28 | 21/28 | 0.766 |
| Xenova/ms-marco-MiniLM-L-12-v2 | 1200 | 5 | 15/36 | 23/36 | 0.592 | 20/28 | 22/28 | 0.759 |
| Xenova/ms-marco-MiniLM-L-12-v2 | 1200 | 8 | 14/36 | 22/36 | 0.553 | 20/28 | 21/28 | 0.764 |
| Xenova/ms-marco-MiniLM-L-12-v2 | 800 | 5 | 13/36 | 25/36 | 0.566 | 19/28 | 21/28 | 0.726 |
| Xenova/ms-marco-MiniLM-L-12-v2 | 800 | 8 | 11/36 | 23/36 | 0.515 | 19/28 | 19/28 | 0.729 |

## Adaptive skip (current model, 2000 chars): skipped = questions that did not call the cross-encoder

| candidates | margin | synthetic hit@1 | hit@3 | MRR | skipped | real hit@1 | hit@3 | MRR | skipped |
|---|---|---|---|---|---|---|---|---|---|
| 5 | 0.0 | 34/36 | 34/36 | 0.944 | 0/36 | 18/28 | 21/28 | 0.720 | 0/28 |
| 5 | 0.1 | 34/36 | 34/36 | 0.944 | 4/36 | 18/28 | 21/28 | 0.720 | 2/28 |
| 5 | 0.2 | 34/36 | 34/36 | 0.944 | 2/36 | 18/28 | 21/28 | 0.720 | 0/28 |
| 5 | 0.3 | 34/36 | 34/36 | 0.944 | 2/36 | 18/28 | 21/28 | 0.720 | 0/28 |
| 5 | 0.4 | 34/36 | 34/36 | 0.944 | 2/36 | 18/28 | 21/28 | 0.720 | 0/28 |
| 5 | 0.5 | 34/36 | 34/36 | 0.944 | 2/36 | 18/28 | 21/28 | 0.720 | 0/28 |
| 5 | 0.6 | 34/36 | 34/36 | 0.944 | 0/36 | 18/28 | 21/28 | 0.720 | 0/28 |
| 5 | 0.8 | 34/36 | 34/36 | 0.944 | 0/36 | 18/28 | 21/28 | 0.720 | 0/28 |
| 8 | 0.0 | 34/36 | 34/36 | 0.944 | 0/36 | 18/28 | 22/28 | 0.738 | 0/28 |
| 8 | 0.1 | 34/36 | 34/36 | 0.944 | 4/36 | 18/28 | 22/28 | 0.738 | 2/28 |
| 8 | 0.2 | 34/36 | 34/36 | 0.944 | 2/36 | 18/28 | 22/28 | 0.738 | 0/28 |
| 8 | 0.3 | 34/36 | 34/36 | 0.944 | 2/36 | 18/28 | 22/28 | 0.738 | 0/28 |
| 8 | 0.4 | 34/36 | 34/36 | 0.944 | 2/36 | 18/28 | 22/28 | 0.738 | 0/28 |
| 8 | 0.5 | 34/36 | 34/36 | 0.944 | 2/36 | 18/28 | 22/28 | 0.738 | 0/28 |
| 8 | 0.6 | 34/36 | 34/36 | 0.944 | 0/36 | 18/28 | 22/28 | 0.738 | 0/28 |
| 8 | 0.8 | 34/36 | 34/36 | 0.944 | 0/36 | 18/28 | 22/28 | 0.738 | 0/28 |

## Latency of one `score()` call (one factor at a time around the default)

| model | chars | ONNX threads | candidates | p50 ms | p95 ms |
|---|---|---|---|---|---|
| BAAI/bge-reranker-v2-m3-int8 | 2000 | default | 5 | 4716 | 7293 |
| jinaai/jina-reranker-v2-base-multilingual | 2000 | default | 5 | 5350 | 6642 |
| Xenova/ms-marco-MiniLM-L-12-v2 | 2000 | default | 5 | 2012 | 2385 |
| BAAI/bge-reranker-v2-m3-int8 | 1200 | default | 5 | 6374 | 10410 |
| BAAI/bge-reranker-v2-m3-int8 | 800 | default | 5 | 3844 | 8551 |
| BAAI/bge-reranker-v2-m3-int8 | 2000 | 4 | 5 | 5346 | 8009 |
| BAAI/bge-reranker-v2-m3-int8 | 2000 | 8 | 5 | 3516 | 5792 |
| BAAI/bge-reranker-v2-m3-int8 | 2000 | default | 8 | 4970 | 6724 |
