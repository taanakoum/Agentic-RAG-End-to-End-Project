"""Authorized hybrid retrieval over Qdrant: dense + sparse search, RRF fusion, cross-encoder rerank.

This class never decides WHO may see what: the caller passes the user id and the exact set of
document ids that user may read (completed, not deleted). Both go into the Qdrant filter of EVERY
query, so unauthorized chunks cannot enter the candidate lists at all. Blocking work (model
inference) runs on a dedicated thread pool, never on the event loop.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from qdrant_client import AsyncQdrantClient
from qdrant_client.models import (
    FieldCondition,
    Filter,
    MatchAny,
    MatchValue,
    ScoredPoint,
    SparseVector,
)

from app.core.config import Settings
from app.core.exceptions import (
    ExternalServiceUnavailableError,
    RetrievalError,
    RetrievalTimeoutError,
)
from app.core.logging import get_logger
from app.core.telemetry import instruments, span, timed
from app.ingestion.embedding import Embedder
from app.ingestion.indexing import collection_name
from app.retrieval.fusion import rrf_fuse
from app.retrieval.models import Hit, RankedChunk, RetrievalMode, RetrievalResult
from app.retrieval.rerank import Reranker

log = get_logger(__name__)

Scored = list[tuple[Hit, float]]


def build_filter(user_id: uuid.UUID, document_ids: Sequence[uuid.UUID]) -> Filter:
    """The authorization filter attached to every query (tenant AND the allowed documents)."""
    return Filter(
        must=[
            FieldCondition(key="user_id", match=MatchValue(value=str(user_id))),
            FieldCondition(
                key="document_id", match=MatchAny(any=[str(d) for d in sorted(set(document_ids))])
            ),
        ]
    )


def _hit_from_point(point: ScoredPoint, user_id: uuid.UUID) -> Hit | None:
    payload: dict[str, Any] = point.payload or {}
    try:
        # Defence in depth: the filter already guarantees this; a mismatch means a bug or a
        # corrupted index, and the chunk must not be used.
        if payload.get("user_id") != str(user_id):
            log.error("retrieval_foreign_chunk_dropped", chunk_id=str(point.id))
            return None
        return Hit(
            chunk_id=str(payload["chunk_id"]),
            document_id=uuid.UUID(payload["document_id"]),
            document_version=int(payload["document_version"]),
            chunk_index=int(payload["chunk_index"]),
            filename=str(payload.get("filename", "")),
            page_number=payload.get("page_number"),
            page_end=payload.get("page_end"),
            section_path=str(payload.get("section_path") or ""),
            heading=payload.get("heading"),
            text=str(payload["text"]),
        )
    except (KeyError, ValueError, TypeError):
        log.error("retrieval_malformed_payload", chunk_id=str(point.id))
        return None


class Retriever:
    def __init__(
        self,
        qdrant: AsyncQdrantClient,
        settings: Settings,
        embedder: Embedder,
        reranker: Reranker,
        executor: ThreadPoolExecutor,
    ) -> None:
        self._qdrant = qdrant
        self._settings = settings
        self._embedder = embedder
        self._reranker = reranker
        self._executor = executor

    async def _offload[T](self, fn: Callable[[], T]) -> T:
        return await asyncio.get_running_loop().run_in_executor(self._executor, fn)

    async def _query(
        self, *, using: str, query: Any, flt: Filter, limit: int, user_id: uuid.UUID
    ) -> Scored:
        response = await self._qdrant.query_points(
            collection_name(self._settings),
            query=query,
            using=using,
            query_filter=flt,
            limit=limit,
            with_payload=True,
            with_vectors=False,
            timeout=max(1, int(self._settings.qdrant_timeout_s)),
        )
        out: Scored = []
        for point in response.points:
            hit = _hit_from_point(point, user_id)
            if hit is not None:
                out.append((hit, float(point.score)))
        return out

    async def search(
        self,
        *,
        user_id: uuid.UUID,
        query: str,
        document_ids: Sequence[uuid.UUID],
        mode: RetrievalMode,
        top_k: int,
    ) -> RetrievalResult:
        """Authorized hybrid search (traced: spans and timings carry counts, never query text)."""
        with (
            span(
                "retrieval.search",
                mode=mode.value,
                top_k=top_k,
                readable_documents=len(document_ids),
            ) as current,
            timed() as total,
        ):
            result = await self._search(
                user_id=user_id, query=query, document_ids=document_ids, mode=mode, top_k=top_k
            )
            current.set_attribute("retrieval.chunks", len(result.chunks))
            for name, count in result.candidates.items():
                current.set_attribute(f"retrieval.candidates.{name}", count)
            if result.degraded:
                current.set_attribute("retrieval.degraded", list(result.degraded))
        meter = instruments().retrieval_latency
        meter.record(total[0], {"stage": "total", "mode": mode.value})
        for stage, ms in result.timings_ms.items():
            if stage != "total":
                meter.record(ms, {"stage": stage, "mode": mode.value})
        return result

    async def _search(
        self,
        *,
        user_id: uuid.UUID,
        query: str,
        document_ids: Sequence[uuid.UUID],
        mode: RetrievalMode,
        top_k: int,
    ) -> RetrievalResult:
        s = self._settings
        started = time.perf_counter()
        result = RetrievalResult(query=query, mode=mode, chunks=[])
        text = query.strip()
        if not text:
            raise RetrievalError("The query is empty.")
        if not document_ids:
            return result  # nothing this user may read: do not even touch the vector store
        flt = build_filter(user_id, document_ids)
        want_dense = mode is not RetrievalMode.SPARSE
        want_sparse = mode is not RetrievalMode.DENSE
        try:
            async with asyncio.timeout(s.retrieval_timeout_s):
                dense_hits, sparse_hits = await self._search_legs(
                    text, flt, user_id, want_dense, want_sparse, result
                )
        except TimeoutError as exc:
            raise RetrievalTimeoutError() from exc
        result.candidates = {"dense": len(dense_hits), "sparse": len(sparse_hits)}

        t0 = time.perf_counter()
        chunks: dict[str, RankedChunk] = {}
        for rank, (hit, score) in enumerate(dense_hits, start=1):
            chunk = chunks.setdefault(hit.chunk_id, RankedChunk(hit=hit))
            chunk.dense_rank, chunk.dense_score = rank, score
        for rank, (hit, score) in enumerate(sparse_hits, start=1):
            chunk = chunks.setdefault(hit.chunk_id, RankedChunk(hit=hit))
            chunk.sparse_rank, chunk.sparse_score = rank, score
        rankings = {
            name: [h.chunk_id for h, _ in hits]
            for name, hits in (("dense", dense_hits), ("sparse", sparse_hits))
            if hits
        }
        fused = rrf_fuse(rankings, k=s.rrf_k)
        for chunk_id, score in fused:
            chunks[chunk_id].fused_score = score
        ordered = [chunks[chunk_id] for chunk_id, _ in fused]
        result.candidates["fused"] = len(ordered)
        result.timings_ms["fuse"] = (time.perf_counter() - t0) * 1000

        if mode is RetrievalMode.HYBRID_RERANK and ordered:
            head = await self._rerank(text, ordered[: s.rerank_candidates], result)
            # the head is reranked; the remaining fused candidates follow in fused order, so the
            # caller still gets top_k chunks when rerank_candidates < top_k
            ordered = head + ordered[s.rerank_candidates :]
        result.chunks = ordered[:top_k]
        result.timings_ms["total"] = (time.perf_counter() - started) * 1000
        return result

    async def _search_legs(
        self,
        text: str,
        flt: Filter,
        user_id: uuid.UUID,
        want_dense: bool,
        want_sparse: bool,
        result: RetrievalResult,
    ) -> tuple[Scored, Scored]:
        s = self._settings
        t0 = time.perf_counter()

        async def dense_leg() -> Scored:
            vec = await self._offload(
                lambda: self._embedder.embed_query_dense(s.dense_query_prefix + text)
            )
            return await self._query(
                using="dense", query=vec, flt=flt, limit=s.retrieval_dense_limit, user_id=user_id
            )

        async def sparse_leg() -> Scored:
            sv = await self._offload(lambda: self._embedder.embed_query_sparse(text))
            return await self._query(
                using="sparse",
                query=SparseVector(indices=sv.indices, values=sv.values),
                flt=flt,
                limit=s.retrieval_sparse_limit,
                user_id=user_id,
            )

        names: list[str] = []
        tasks: list[Any] = []
        if want_dense:
            names.append("dense")
            tasks.append(dense_leg())
        if want_sparse:
            names.append("sparse")
            tasks.append(sparse_leg())
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)
        hits: dict[str, Scored] = {"dense": [], "sparse": []}
        failures: list[BaseException] = []
        for name, outcome in zip(names, outcomes, strict=True):
            if isinstance(outcome, BaseException):
                if isinstance(outcome, asyncio.CancelledError):
                    raise outcome
                log.warning("retrieval_leg_failed", leg=name, error=type(outcome).__name__)
                failures.append(outcome)
                result.degraded.append(f"{name}_unavailable")
            else:
                hits[name] = outcome
        if failures and len(failures) == len(names):
            raise ExternalServiceUnavailableError() from failures[0]
        result.timings_ms["search"] = (time.perf_counter() - t0) * 1000
        return hits["dense"], hits["sparse"]

    async def _rerank(
        self, text: str, candidates: list[RankedChunk], result: RetrievalResult
    ) -> list[RankedChunk]:
        with span("retrieval.rerank", candidates=len(candidates)) as current:
            ranked = await self._rerank_impl(text, candidates, result)
            current.set_attribute(
                "retrieval.reranked", any(c.rerank_score is not None for c in ranked)
            )
            return ranked

    async def _rerank_impl(
        self, text: str, candidates: list[RankedChunk], result: RetrievalResult
    ) -> list[RankedChunk]:
        s = self._settings
        t0 = time.perf_counter()
        limit = s.rerank_max_chars
        passages = [
            (f"{c.hit.section_path}\n{c.hit.text}" if c.hit.section_path else c.hit.text)[:limit]
            for c in candidates
        ]
        try:
            scores = await asyncio.wait_for(
                self._offload(lambda: self._reranker.score(text, passages)),
                timeout=s.rerank_timeout_s,
            )
            if len(scores) != len(candidates):
                raise ValueError("reranker returned a wrong number of scores")
        except TimeoutError:
            log.warning("rerank_timeout", timeout_s=s.rerank_timeout_s)
            result.degraded.append("rerank_timeout")
            return candidates
        except Exception as exc:  # reranking is an enhancement: fall back to the fused order
            log.warning("rerank_failed", error=type(exc).__name__)
            result.degraded.append("rerank_unavailable")
            return candidates
        finally:
            result.timings_ms["rerank"] = (time.perf_counter() - t0) * 1000
        for chunk, score in zip(candidates, scores, strict=True):
            chunk.rerank_score = float(score)
        # stable: equal rerank scores keep their fused order
        return sorted(candidates, key=lambda c: -(c.rerank_score or 0.0))
