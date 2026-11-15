"""Retrieval for one user: authorization first, then hybrid search, then re-verification.

Order of events (docs/spec/04-retrieval.md "Authorization"):
1. PostgreSQL says which documents this user may read (owned, COMPLETED, not deleted),
   optionally narrowed to the ids the caller asked for. Unknown / foreign ids simply vanish.
2. Qdrant is queried with the user id AND that document set on every query (dense and sparse).
3. Right before returning, the hits are checked again against PostgreSQL (the document may have
   been deleted or re-indexed while the search ran); stale hits are dropped.
Only chunks that survive step 3 can ever reach a prompt or a citation.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor

from qdrant_client import AsyncQdrantClient
from redis.asyncio import Redis as AsyncRedis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import ErrorDetail, RequestValidationAppError
from app.core.logging import get_logger
from app.infra.rate_limit import RateLimiter, subject_key
from app.ingestion.embedding import get_embedder
from app.repositories.documents import DocumentRepository
from app.retrieval.cache import RetrievalCache, knowledge_base_fingerprint
from app.retrieval.models import RankedChunk, RetrievalMode, RetrievalResult
from app.retrieval.rerank import get_reranker
from app.retrieval.retriever import Retriever
from app.services.auth_service import AuthenticatedUser

log = get_logger(__name__)


class RetrievalService:
    def __init__(
        self,
        *,
        session: AsyncSession,
        settings: Settings,
        qdrant: AsyncQdrantClient,
        executor: ThreadPoolExecutor,
        limiter: RateLimiter,
        redis: AsyncRedis | None = None,
    ) -> None:
        self._cache = RetrievalCache(redis, settings) if redis is not None else None
        self._settings = settings
        self._docs = DocumentRepository(session)
        self._limiter = limiter
        self._qdrant = qdrant
        self._executor = executor

    def default_mode(self) -> RetrievalMode:
        s = self._settings
        if s.rerank_enabled:
            return RetrievalMode.HYBRID_RERANK
        if s.retrieval_mode_without_rerank == "dense":
            return RetrievalMode.DENSE
        return RetrievalMode.HYBRID

    async def search(
        self,
        user: AuthenticatedUser,
        query: str,
        *,
        document_ids: Sequence[uuid.UUID] | None = None,
        top_k: int | None = None,
        mode: RetrievalMode | None = None,
        rate_limited: bool = True,
    ) -> RetrievalResult:
        s = self._settings
        if not query.strip():
            raise RequestValidationAppError(
                details=[ErrorDetail(field="query", code="query_empty", message="Enter a query.")]
            )
        if len(query) > s.retrieval_max_query_chars:
            raise RequestValidationAppError(
                details=[
                    ErrorDetail(
                        field="query",
                        code="query_too_long",
                        message=f"At most {s.retrieval_max_query_chars} characters.",
                    )
                ]
            )
        if document_ids is not None and len(document_ids) > s.retrieval_max_scope_documents:
            raise RequestValidationAppError(
                details=[
                    ErrorDetail(
                        field="document_ids",
                        code="too_many_documents",
                        message=f"At most {s.retrieval_max_scope_documents} documents at once.",
                    )
                ]
            )
        if rate_limited:
            await self._limiter.hit(
                "search",
                subject_key(str(user.id)),
                limit=s.rl_search_limit,
                window_s=s.rl_search_window_s,
            )
        k = min(top_k or s.retrieval_top_k, s.retrieval_max_top_k)
        readable = await self._docs.readable(user.id, document_ids)
        retriever = Retriever(
            self._qdrant,
            s,
            get_embedder(s),
            get_reranker(s),
            self._executor,
            cache=self._cache,
        )
        result = await retriever.search(
            user_id=user.id,
            query=query,
            document_ids=list(readable),
            mode=mode or self.default_mode(),
            top_k=k,
            knowledge_base=knowledge_base_fingerprint(readable),
        )
        result.readable_documents = len(readable)
        if not result.chunks:
            return result
        # Re-verify against the database: deleted, re-indexed or foreign documents are dropped.
        current = await self._docs.readable(user.id, [c.hit.document_id for c in result.chunks])
        kept: list[RankedChunk] = []
        for chunk in result.chunks:
            info = current.get(chunk.hit.document_id)
            if info is None or info[1] != chunk.hit.document_version:
                log.info("retrieval_stale_hit_dropped", chunk_id=chunk.hit.chunk_id)
                continue
            chunk.document_name = info[0]
            kept.append(chunk)
        result.chunks = kept
        return result
