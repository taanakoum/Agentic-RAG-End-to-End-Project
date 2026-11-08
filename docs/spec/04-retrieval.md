# 04 Hybrid Retrieval, Reranking, Citations

Core component; give it real engineering and evaluation effort. Build as an independently testable service.

## Dense
Configurable embedding provider/model, batching, consistent dims for doc and query, normalization where required, model-version tracking (stored in chunk payload), retries/error handling.

## Sparse
Evaluate BM25 / Qdrant sparse vectors / SPLADE-style. Pick one that integrates with Qdrant; document trade-offs (quality vs compute). Index at ingestion; query at search time.

## Fusion
RRF: score(d) = sum over rankers 1/(k + rank_r(d)), k configurable. Dedupe, metadata filtering, configurable depths, retrieval timeouts. Verify BOTH dense and sparse candidates reach the fused set. Evaluate alternatives if justified.

## Cross-encoder
Rerank fused candidates. Configurable model, candidate count, final top-k; batched inference; CPU fallback; timeouts; latency tracking; compare against reranking disabled. Run inference off the event loop. MMR / contextual compression / parent-child only if evaluation shows benefit.

## Authorization
User/tenant (and optional document) filters on EVERY Qdrant query (dense and sparse) and only COMPLETED, non-deleted documents. Filtering happens before evidence can reach the LLM.

## Query processing
Query rewriting/expansion where beneficial and evaluated; retrieval necessity detection upstream in the graph.

## Citations
Citation fields: document_id, document_name, page_number, chunk_id, excerpt.
Backend verification for every citation: (1) document exists (2) chunk exists (3) chunk belongs to cited document (4) current user has access (5) page number matches stored metadata (6) chunk was in the authorized evidence set retrieved in THIS execution. Never trust LLM-produced IDs/pages/excerpts; excerpt should be derived from/validated against the stored chunk text.
On failure: reject citation, bounded correction attempt, remove unsupported refs, safe fallback. Distinguish directly supported / reasonable inference / not found in KB.

## Evaluation of retrieval (see 07)
Recall@K, Precision@K, HitRate@K, MRR, NDCG across: dense, sparse, hybrid, hybrid+RRF, hybrid+rerank. Inspect passages manually on representative queries. Do not claim success just because both retrievers return results. Datasets must not be artificially easy; never fabricate ground truth.
