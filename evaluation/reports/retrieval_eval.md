# Retrieval evaluation (synthetic corpus, real models)

Dataset `retrieval-synth-v1`: 6 documents, 43 pages, 72 chunks (production Docling pipeline), 36 questions in two styles (natural question; AUTO-DERIVED keyword query = the section heading). Relevant = chunk containing the answer sentence.

Models: dense `BAAI/bge-small-en-v1.5`, sparse `Qdrant/bm25`, reranker `BAAI/bge-reranker-v2-m3-int8`. Depth: dense/sparse 40 each, RRF k=60, final top-10. Hardware: Intel64 Family 6 Model 140 Stepping 1, GenuineIntel, Windows CPU only. Date: 2026-11-08.

HONEST LIMITS: small synthetic corpus; ranks configurations on this data only. Differences of a few points on 36 questions are within noise (one question = 2.8 points).

## Query style: natural

| Configuration | hit@1 | hit@3 | hit@5 | recall@5 | prec@3 | MRR | NDCG@5 | NDCG@10 | median ms | p95 ms |
|---|---|---|---|---|---|---|---|---|---|---|
| dense | 0.58 | 0.72 | 0.83 | 0.83 | 0.24 | 0.69 | 0.72 | 0.74 | 9 | 11 |
| sparse (BM25) | 0.53 | 0.81 | 0.86 | 0.86 | 0.27 | 0.66 | 0.71 | 0.72 | 3 | 5 |
| hybrid (RRF k=60) | 0.72 | 0.92 | 0.94 | 0.94 | 0.31 | 0.82 | 0.85 | 0.85 | 10 | 12 |
| hybrid + rerank (DEFAULT: bge-reranker-v2-m3 int8, 5 candidates) | 0.94 | 0.94 | 0.94 | 0.94 | 0.31 | 0.94 | 0.94 | 0.94 | 2170 | 2480 |
| dense + BGE query instruction | 0.50 | 0.78 | 0.83 | 0.83 | 0.26 | 0.66 | 0.69 | 0.73 | 10 | 12 |
| hybrid (RRF k=10) | 0.72 | 0.89 | 0.94 | 0.94 | 0.30 | 0.81 | 0.85 | 0.85 | 10 | 12 |
| hybrid + rerank: ms-marco-MiniLM-L-6, 30 candidates | 0.42 | 0.83 | 0.89 | 0.89 | 0.28 | 0.63 | 0.69 | 0.71 | 1500 | 1659 |
| hybrid + rerank: ms-marco-MiniLM-L-6, 10 candidates | 0.47 | 0.86 | 0.92 | 0.92 | 0.29 | 0.67 | 0.73 | 0.74 | 465 | 525 |
| hybrid + rerank: ms-marco-MiniLM-L-12, 30 candidates | 0.81 | 0.89 | 0.89 | 0.89 | 0.30 | 0.85 | 0.85 | 0.87 | 2859 | 3162 |
| hybrid + rerank: jina-reranker-v1-turbo-en, 30 candidates | 0.69 | 0.89 | 0.89 | 0.89 | 0.30 | 0.79 | 0.81 | 0.82 | 1869 | 2124 |
| hybrid + rerank: bge-reranker-v2-m3 (int8), 30 candidates | 0.97 | 1.00 | 1.00 | 1.00 | 0.33 | 0.99 | 0.99 | 0.99 | 14378 | 15551 |
| hybrid + rerank: bge-reranker-v2-m3 (int8), 10 candidates | 0.94 | 0.94 | 0.94 | 0.94 | 0.31 | 0.94 | 0.94 | 0.94 | 4327 | 5054 |

## Query style: heading_keywords

| Configuration | hit@1 | hit@3 | hit@5 | recall@5 | prec@3 | MRR | NDCG@5 | NDCG@10 | median ms | p95 ms |
|---|---|---|---|---|---|---|---|---|---|---|
| dense | 0.86 | 0.94 | 1.00 | 1.00 | 0.31 | 0.92 | 0.94 | 0.94 | 9 | 11 |
| sparse (BM25) | 0.14 | 1.00 | 1.00 | 1.00 | 0.33 | 0.57 | 0.68 | 0.68 | 3 | 5 |
| hybrid (RRF k=60) | 0.78 | 1.00 | 1.00 | 1.00 | 0.33 | 0.89 | 0.92 | 0.92 | 10 | 12 |
| hybrid + rerank (DEFAULT: bge-reranker-v2-m3 int8, 5 candidates) | 0.89 | 1.00 | 1.00 | 1.00 | 0.33 | 0.94 | 0.96 | 0.96 | 2170 | 2480 |
| dense + BGE query instruction | 0.86 | 0.97 | 0.97 | 0.97 | 0.32 | 0.92 | 0.93 | 0.94 | 10 | 12 |
| hybrid (RRF k=10) | 0.78 | 1.00 | 1.00 | 1.00 | 0.33 | 0.89 | 0.92 | 0.92 | 10 | 12 |
| hybrid + rerank: ms-marco-MiniLM-L-6, 30 candidates | 0.14 | 1.00 | 1.00 | 1.00 | 0.33 | 0.57 | 0.68 | 0.68 | 1500 | 1659 |
| hybrid + rerank: ms-marco-MiniLM-L-6, 10 candidates | 0.17 | 1.00 | 1.00 | 1.00 | 0.33 | 0.58 | 0.69 | 0.69 | 465 | 525 |
| hybrid + rerank: ms-marco-MiniLM-L-12, 30 candidates | 0.50 | 1.00 | 1.00 | 1.00 | 0.33 | 0.75 | 0.82 | 0.82 | 2859 | 3162 |
| hybrid + rerank: jina-reranker-v1-turbo-en, 30 candidates | 0.36 | 1.00 | 1.00 | 1.00 | 0.33 | 0.68 | 0.76 | 0.76 | 1869 | 2124 |
| hybrid + rerank: bge-reranker-v2-m3 (int8), 30 candidates | 0.78 | 1.00 | 1.00 | 1.00 | 0.33 | 0.89 | 0.92 | 0.92 | 14378 | 15551 |
| hybrid + rerank: bge-reranker-v2-m3 (int8), 10 candidates | 0.83 | 1.00 | 1.00 | 1.00 | 0.33 | 0.92 | 0.94 | 0.94 | 4327 | 5054 |

## Query style: all

| Configuration | hit@1 | hit@3 | hit@5 | recall@5 | prec@3 | MRR | NDCG@5 | NDCG@10 | median ms | p95 ms |
|---|---|---|---|---|---|---|---|---|---|---|
| dense | 0.72 | 0.83 | 0.92 | 0.92 | 0.28 | 0.80 | 0.83 | 0.84 | 9 | 11 |
| sparse (BM25) | 0.33 | 0.90 | 0.93 | 0.93 | 0.30 | 0.61 | 0.70 | 0.70 | 3 | 5 |
| hybrid (RRF k=60) | 0.75 | 0.96 | 0.97 | 0.97 | 0.32 | 0.85 | 0.88 | 0.88 | 10 | 12 |
| hybrid + rerank (DEFAULT: bge-reranker-v2-m3 int8, 5 candidates) | 0.92 | 0.97 | 0.97 | 0.97 | 0.32 | 0.94 | 0.95 | 0.95 | 2170 | 2480 |
| dense + BGE query instruction | 0.68 | 0.88 | 0.90 | 0.90 | 0.29 | 0.79 | 0.81 | 0.83 | 10 | 12 |
| hybrid (RRF k=10) | 0.75 | 0.94 | 0.97 | 0.97 | 0.31 | 0.85 | 0.88 | 0.88 | 10 | 12 |
| hybrid + rerank: ms-marco-MiniLM-L-6, 30 candidates | 0.28 | 0.92 | 0.94 | 0.94 | 0.31 | 0.60 | 0.68 | 0.69 | 1500 | 1659 |
| hybrid + rerank: ms-marco-MiniLM-L-6, 10 candidates | 0.32 | 0.93 | 0.96 | 0.96 | 0.31 | 0.63 | 0.71 | 0.72 | 465 | 525 |
| hybrid + rerank: ms-marco-MiniLM-L-12, 30 candidates | 0.65 | 0.94 | 0.94 | 0.94 | 0.31 | 0.80 | 0.83 | 0.84 | 2859 | 3162 |
| hybrid + rerank: jina-reranker-v1-turbo-en, 30 candidates | 0.53 | 0.94 | 0.94 | 0.94 | 0.31 | 0.74 | 0.79 | 0.79 | 1869 | 2124 |
| hybrid + rerank: bge-reranker-v2-m3 (int8), 30 candidates | 0.88 | 1.00 | 1.00 | 1.00 | 0.33 | 0.94 | 0.95 | 0.95 | 14378 | 15551 |
| hybrid + rerank: bge-reranker-v2-m3 (int8), 10 candidates | 0.89 | 0.97 | 0.97 | 0.97 | 0.32 | 0.93 | 0.94 | 0.94 | 4327 | 5054 |

