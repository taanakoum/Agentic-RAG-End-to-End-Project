# Retrieval evaluation on real public documents

Dataset `real-docs-v1`: NIST SP 800-63B Digital Identity Guidelines: Authentication and Lifecycle Management, Attention Is All You Need (Vaswani et al., 2017, arXiv:1706.03762v7). 276 chunks from the production Docling pipeline, 28 scoreable questions (28 written). Relevant = a chunk containing the answer sentence copied from the document. The documents themselves are not in the repository (`python evaluation/retrieval/real_docs.py --download` fetches and hash-checks them).

Models: dense `BAAI/bge-small-en-v1.5`, sparse `Qdrant/bm25`, reranker `BAAI/bge-reranker-v2-m3-int8` (5 candidates). Depth: dense/sparse 40 each, RRF k=60, top-10. Hardware: Intel64 Family 6 Model 140 Stepping 1, GenuineIntel, Windows CPU only. Date: 2026-11-13.

HONEST LIMITS: two documents, one author of questions, one embedding model, one machine. One question = 3.6 points, so differences of one or two questions are noise. Questions were written after reading the text and are not controlled for word overlap with the answer sentence. The ground truth is strict (one sentence = one chunk): when the document states the same rule in two places, a hit on the other place counts as a miss, so absolute numbers are pessimistic.

| Configuration | hit@1 | hit@3 | hit@5 | hit@10 | recall@5 | MRR | NDCG@5 | median ms | p95 ms |
|---|---|---|---|---|---|---|---|---|---|
| dense | 0.61 | 0.71 | 0.79 | 0.82 | 0.79 | 0.68 | 0.71 | 13 | 15 |
| sparse (BM25) | 0.39 | 0.54 | 0.68 | 0.79 | 0.68 | 0.51 | 0.54 | 6 | 7 |
| hybrid (RRF k=60) | 0.46 | 0.68 | 0.79 | 0.89 | 0.79 | 0.61 | 0.64 | 15 | 19 |
| hybrid + rerank (DEFAULT) | 0.64 | 0.75 | 0.79 | 0.89 | 0.79 | 0.72 | 0.73 | 2913 | 4157 |

## Per document (hit@1 / hit@3 / MRR)

| Configuration | attention-is-all-you-need | nist-sp-800-63b |
|---|---|---|
| dense | 0.85 / 0.92 / 0.88 (n=13) | 0.40 / 0.53 / 0.51 (n=15) |
| sparse (BM25) | 0.54 / 0.77 / 0.68 (n=13) | 0.27 / 0.33 / 0.36 (n=15) |
| hybrid (RRF k=60) | 0.69 / 0.85 / 0.78 (n=13) | 0.27 / 0.53 / 0.45 (n=15) |
| hybrid + rerank (DEFAULT) | 0.85 / 0.92 / 0.88 (n=13) | 0.47 / 0.60 / 0.58 (n=15) |

## Questions where the configurations disagree (first relevant rank)

| id | question | dense | sparse (BM25) | hybrid (RRF k=60) | hybrid + rerank (DEFAULT) |
|---|---|---|---|---|---|
| r01 | What is the minimum length for a password that the user picks themselves? | - | - | - | - |
| r03 | Does the standard want people to change their passwords on a schedule? | 4 | 7 | 2 | 2 |
| r04 | How should passwords be stored by the service? | - | 6 | 8 | 8 |
| r07 | After how much idle time must a user at the highest assurance level authenticate again? | 4 | - | 6 | 6 |
| r08 | How long can an AAL1 session last before the person has to sign in again? | 2 | 4 | 2 | 1 |
| r09 | Is it acceptable to send one-time codes by phone call or text message? | - | - | - | - |
| r10 | Should the sign-in form let people paste their password? | 6 | 6 | 4 | 1 |
| r11 | Should services force users to include symbols and uppercase letters in passwords? | - | 5 | 8 | 8 |
| r13 | How much randomness must a code sent over a separate channel have at least? | 2 | - | 4 | 4 |
| r14 | What delay must follow failed fingerprint or face attempts? | 1 | 2 | 2 | 1 |
| r15 | What is the appendix's view of other protections compared with strict complexity requirements? | 1 | - | 3 | 2 |
| r16 | How many identical layers make up the encoder? | 1 | 5 | 1 | 1 |
| r17 | How many attention heads does the base model use? | 1 | - | 5 | 2 |
| r20 | How big was the English-German training set? | 1 | 3 | 2 | 1 |
| r22 | How long did training of the smaller models take? | 1 | 4 | 1 | 1 |
| r24 | What translation quality score did the largest model reach on English to German? | 1 | 2 | 1 | 1 |
| r25 | How large a vocabulary was used for the parsing experiments with extra unlabeled data? | 2 | 1 | 1 | 1 |
| r26 | How are attention weights computed from queries and keys? | 1 | 2 | 2 | 1 |
| r27 | What do the authors claim is new about their model in the conclusion? | - | 1 | - | - |
