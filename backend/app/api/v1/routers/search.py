"""Search endpoint: hybrid retrieval over the caller's own completed documents."""

from __future__ import annotations

from fastapi import APIRouter

from app.core.exceptions import error_responses
from app.dependencies.auth import RetrievalServiceDep, VerifiedUser
from app.schemas.search import SearchChunk, SearchRequest, SearchResponse, SearchScores

router = APIRouter(prefix="/search", tags=["search"])


@router.post(
    "",
    response_model=SearchResponse,
    responses=error_responses(401, 403, 422, 429, 500, 503, 504),
)
async def search(
    body: SearchRequest, current: VerifiedUser, service: RetrievalServiceDep
) -> SearchResponse:
    """Dense + sparse retrieval, RRF fusion and cross-encoder reranking, scoped to the caller."""
    result = await service.search(
        current, body.query, document_ids=body.document_ids, top_k=body.top_k
    )
    return SearchResponse(
        query=result.query,
        mode=result.mode.value,
        results=[
            SearchChunk(
                chunk_id=c.hit.chunk_id,
                document_id=c.hit.document_id,
                document_name=c.document_name,
                page_number=c.hit.page_number,
                page_end=c.hit.page_end,
                section_path=c.hit.section_path,
                heading=c.hit.heading,
                text=c.hit.text,
                scores=SearchScores(
                    dense_rank=c.dense_rank,
                    sparse_rank=c.sparse_rank,
                    dense_score=c.dense_score,
                    sparse_score=c.sparse_score,
                    fused_score=c.fused_score,
                    rerank_score=c.rerank_score,
                ),
            )
            for c in result.chunks
        ],
        degraded=result.degraded,
        timings_ms={k: round(v, 2) for k, v in result.timings_ms.items()},
    )
