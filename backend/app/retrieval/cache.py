"""Redis cache for retrieval results, per user, per question and per knowledge-base version.

What is cached: only the ORDER and the scores of the final chunks (chunk ids + rank/score
numbers), never chunk text. On a hit the chunk payloads are loaded again from Qdrant by id with the
same owner/document filter as a normal search (see `Retriever`), so deleted or foreign chunks can
never come out of the cache and the cache never holds document content.

Design rules (docs/ARCHITECTURE_DECISIONS.md, ADR-036):
  * Key `retr:v1:u:<hashed user>:<sha256>`; the digest covers the normalised query, the mode, top-k,
    the retrieval/rerank settings that change the ranking and the KNOWLEDGE-BASE FINGERPRINT: a hash
    of the (document id, version) pairs the caller may read. A new version, an upload that finishes,
    a deletion or a different scope therefore changes the key (nothing stale is ever served).
  * Namespaced by the hashed user id: entries are never shared between users, and are removed with
    the account (`purge_user_retrieval_cache*`).
  * Fail-open with a short Redis timeout: any problem is a miss, never a failed question.
  * Degraded results (a leg or the reranker failed) and empty results are not stored.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import unicodedata
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from redis import Redis
from redis.asyncio import Redis as AsyncRedis
from redis.exceptions import RedisError

from app.core.config import Settings
from app.core.logging import get_logger
from app.infra.rate_limit import subject_key
from app.retrieval.models import RankedChunk, RetrievalMode, RetrievalResult

log = get_logger(__name__)

KEY_VERSION = "v1"
KEY_PREFIX = "retr"
_FIELDS = (
    ("dense_rank", "dr"),
    ("sparse_rank", "sr"),
    ("dense_score", "ds"),
    ("sparse_score", "ss"),
    ("fused_score", "fs"),
    ("rerank_score", "rs"),
)


def normalize_query(query: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", query).casefold().split())


def knowledge_base_fingerprint(readable: Mapping[uuid.UUID, tuple[str, int]]) -> str:
    """Hash of the readable (document id, version) pairs (file names are not part of it)."""
    pairs = sorted((str(doc_id), int(info[1])) for doc_id, info in readable.items())
    return hashlib.sha256(json.dumps(pairs, separators=(",", ":")).encode("utf-8")).hexdigest()


def user_pattern(user_id: uuid.UUID) -> str:
    return f"{KEY_PREFIX}:{KEY_VERSION}:u:{subject_key(str(user_id))}:*"


@dataclass(frozen=True, slots=True)
class CachedChunk:
    chunk_id: str
    numbers: dict[str, float | int]


@dataclass(frozen=True, slots=True)
class CachedResult:
    chunks: list[CachedChunk]
    candidates: dict[str, int]


class RetrievalCache:
    def __init__(self, redis: AsyncRedis, settings: Settings) -> None:
        self._redis = redis
        self._settings = settings

    @property
    def enabled(self) -> bool:
        return self._settings.retrieval_cache_enabled

    def key(
        self,
        *,
        user_id: uuid.UUID,
        query: str,
        mode: RetrievalMode,
        top_k: int,
        knowledge_base: str,
    ) -> str:
        s = self._settings
        material: dict[str, Any] = {
            "query": normalize_query(query),
            "mode": mode.value,
            "top_k": top_k,
            "kb": knowledge_base,
            "config": [
                s.embedding_model,
                s.sparse_model,
                s.reranker_model if mode is RetrievalMode.HYBRID_RERANK else "",
                s.retrieval_dense_limit,
                s.retrieval_sparse_limit,
                s.rrf_k,
                s.rerank_candidates,
                s.rerank_max_chars,
                s.dense_query_prefix,
            ],
        }
        digest = hashlib.sha256(
            json.dumps(material, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
        return f"{KEY_PREFIX}:{KEY_VERSION}:u:{subject_key(str(user_id))}:{digest}"

    async def get(self, key: str) -> CachedResult | None:
        """The stored ranking, or None (miss, disabled, Redis problem or an unreadable entry)."""
        try:
            async with asyncio.timeout(self._settings.retrieval_cache_timeout_s):
                raw = await self._redis.get(key)
        except (RedisError, OSError, TimeoutError) as exc:
            log.warning("retrieval_cache_read_failed", error=type(exc).__name__)
            return None
        if raw is None:
            return None
        try:
            data = json.loads(raw)
            chunks = [
                CachedChunk(
                    chunk_id=str(item["id"]),
                    numbers={k: item[k] for k in (short for _, short in _FIELDS) if k in item},
                )
                for item in data["chunks"]
            ]
            candidates = {str(k): int(v) for k, v in data["candidates"].items()}
        except (ValueError, KeyError, TypeError, AttributeError):
            log.warning("retrieval_cache_entry_unreadable")
            return None
        return CachedResult(chunks=chunks, candidates=candidates)

    async def put(self, key: str, result: RetrievalResult) -> bool:
        if result.degraded or not result.chunks:
            return False
        chunks: list[dict[str, Any]] = []
        for chunk in result.chunks:
            item: dict[str, Any] = {"id": chunk.hit.chunk_id}
            for attribute, short in _FIELDS:
                value = getattr(chunk, attribute)
                if value is not None:
                    item[short] = value
            chunks.append(item)
        payload = json.dumps({"chunks": chunks, "candidates": result.candidates})
        if len(payload.encode("utf-8")) > self._settings.retrieval_cache_max_bytes:
            return False
        try:
            async with asyncio.timeout(self._settings.retrieval_cache_timeout_s):
                await self._redis.set(key, payload, ex=self._settings.retrieval_cache_ttl_s)
        except (RedisError, OSError, TimeoutError) as exc:
            log.warning("retrieval_cache_store_failed", error=type(exc).__name__)
            return False
        return True


def apply_numbers(chunk: RankedChunk, numbers: Mapping[str, float | int]) -> None:
    """Put the cached ranks and scores back on a chunk."""
    for attribute, short in _FIELDS:
        if short in numbers:
            setattr(chunk, attribute, numbers[short])


async def purge_user_retrieval_cache(redis: AsyncRedis, user_id: uuid.UUID) -> int:
    keys: list[bytes] = []
    async for key in redis.scan_iter(match=user_pattern(user_id), count=500):  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]
        keys.append(key)  # pyright: ignore[reportUnknownArgumentType]
    return int(await redis.delete(*keys)) if keys else 0


def purge_user_retrieval_cache_sync(redis: Redis, user_id: uuid.UUID) -> int:  # pyright: ignore[reportMissingTypeArgument]
    keys: list[bytes] = []
    for key in redis.scan_iter(match=user_pattern(user_id), count=500):  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]
        keys.append(key)  # pyright: ignore[reportUnknownArgumentType]
    return int(redis.delete(*keys)) if keys else 0  # pyright: ignore[reportArgumentType]
