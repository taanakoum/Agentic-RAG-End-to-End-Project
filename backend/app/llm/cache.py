"""Redis cache for LLM calls (frequent prompts -> stored answers), fail-open and tenant-safe.

Design rules (docs/IMPLEMENTATION_PLAN.md "Phase 6 plan"):
  * The key is a SHA-256 of everything that influences the answer: provider host, configured
    models, schema name/version, temperature, token limit, JSON mode and the FULL message list.
    Retrieved chunks, history and memories are part of the messages, so changing or deleting a
    document, or editing a memory, changes the key: nothing stale is ever served.
  * Namespace: prompts that contain user data live under `llm:v1:u:<user_id>:...` and are never
    shared; only context-free prompts (scope "global") share `llm:v1:g:...`.
  * Only output that PASSED validation is stored, and only when the caller says so
    (`LLMGateway.remember`). Failed, invalid, degraded or insufficient-evidence results are not.
  * Fail-open: any Redis problem is a miss, never a failed chat.
  * A hit is not trusted blindly either: the agent still re-verifies every citation afterwards.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import time
import uuid
from collections.abc import AsyncGenerator, Awaitable
from contextlib import asynccontextmanager
from typing import Any

from redis import Redis
from redis.asyncio import Redis as AsyncRedis
from redis.exceptions import RedisError

from app.core.config import Settings
from app.core.exceptions import ModelRateLimitedError
from app.core.logging import get_logger
from app.infra.rate_limit import subject_key
from app.llm.base import ChatModel, LLMRequest, LLMResponse, Usage

log = get_logger(__name__)

KEY_VERSION = "v1"
KEY_PREFIX = "llmc"
_STATS_KEY = f"{KEY_PREFIX}:stats"
# bump when the agent prompts change meaning, so old cached answers are never reused
PROMPT_VERSION = "1"


def namespace(request: LLMRequest) -> str:
    if request.cache_scope == "global":
        return "g"
    if request.user_id is None:
        raise ValueError("a user-scoped prompt needs a user_id")
    return f"u:{subject_key(str(request.user_id))}"  # hashed: no user ids in Redis keys


def cache_key(request: LLMRequest, settings: Settings) -> str:
    material: dict[str, Any] = {
        "prompt_version": PROMPT_VERSION,
        "host": settings.llm_base_url.rstrip("/"),
        "models": [settings.llm_model, *settings.llm_fallback_models],
        "schema": [request.schema_name, request.schema_version, request.json_schema],
        "json_mode": settings.llm_json_mode,
        "temperature": settings.llm_temperature
        if request.temperature is None
        else request.temperature,
        "max_tokens": request.max_tokens or settings.llm_max_tokens,
        "messages": [[m.role, m.content] for m in request.messages],
    }
    digest = hashlib.sha256(
        json.dumps(material, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    return f"{KEY_PREFIX}:{KEY_VERSION}:{namespace(request)}:{digest}"


class _UserSlots:
    """Per-user concurrency limit for model calls (one user cannot drain the free quota)."""

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._slots: dict[uuid.UUID, list[Any]] = {}  # user -> [semaphore, active users]

    @asynccontextmanager
    async def slot(self, user_id: uuid.UUID | None, timeout_s: float) -> AsyncGenerator[None]:
        if user_id is None:
            yield
            return
        entry = self._slots.setdefault(user_id, [asyncio.Semaphore(self._limit), 0])
        entry[1] += 1
        try:
            try:
                async with asyncio.timeout(timeout_s):
                    await entry[0].acquire()
            except TimeoutError as exc:
                raise ModelRateLimitedError(5) from exc
            try:
                yield
            finally:
                entry[0].release()
        finally:
            entry[1] -= 1
            if entry[1] <= 0:
                self._slots.pop(user_id, None)


class LLMGateway:
    """The only way application code calls a model: cache lookup -> model -> (later) remember.

    Every Redis operation is bounded by `LLM_CACHE_TIMEOUT_S` and fails open (a miss).
    """

    def __init__(self, model: ChatModel, redis: AsyncRedis, settings: Settings) -> None:
        self._model = model
        self._redis = redis
        self._settings = settings
        self._slots = _UserSlots(settings.llm_max_concurrency_per_user)

    @property
    def model_name(self) -> str:
        return self._model.model_name

    @property
    def configured(self) -> bool:
        return bool(getattr(self._model, "configured", True))

    async def _call_model(self, request: LLMRequest) -> LLMResponse:
        async with self._slots.slot(request.user_id, self._settings.llm_queue_timeout_s):
            return await self._model.complete(request)

    async def _redis_call(self, call: Awaitable[Any]) -> Any:
        async with asyncio.timeout(self._settings.llm_cache_timeout_s):
            return await call

    async def _count(self, outcome: str) -> None:
        # statistics are best effort
        with contextlib.suppress(RedisError, OSError, TimeoutError):
            await self._redis_call(self._redis.hincrby(_STATS_KEY, outcome, 1))  # pyright: ignore[reportGeneralTypeIssues]

    async def stats(self) -> dict[str, int]:
        try:
            raw: dict[Any, Any] = await self._redis_call(self._redis.hgetall(_STATS_KEY))  # pyright: ignore[reportGeneralTypeIssues]
        except (RedisError, OSError, TimeoutError):
            return {}
        return {(k.decode() if isinstance(k, bytes) else str(k)): int(v) for k, v in raw.items()}

    async def _drop(self, key: str) -> None:
        with contextlib.suppress(RedisError, OSError, TimeoutError):
            await self._redis_call(self._redis.delete(key))

    async def complete(self, request: LLMRequest) -> LLMResponse:
        s = self._settings
        if not (s.llm_cache_enabled and request.use_cache):
            response = await self._call_model(request)
            response.cache = "bypass"
            await self._count("bypass")
            return response
        key = cache_key(request, s)
        try:
            raw = await self._redis_call(self._redis.get(key))
        except (RedisError, OSError, TimeoutError) as exc:
            log.warning("llm_cache_unavailable", error=type(exc).__name__)
            response = await self._call_model(request)
            response.cache = "error"
            await self._count("error")
            return response
        if raw is not None:
            try:
                data = json.loads(raw)
                usage = data.get("usage") or [0, 0]
                response = LLMResponse(
                    text=str(data["text"]),
                    model=str(data.get("model", self._model.model_name)),
                    usage=Usage(int(usage[0]), int(usage[1])),
                    cache="hit",
                    cache_key=key,
                )
            except (ValueError, KeyError, TypeError, IndexError):
                log.warning("llm_cache_entry_unreadable")
                await self._drop(key)  # a broken entry must not keep costing a lookup
            else:
                await self._count("hit")
                return response
        response = await self._call_model(request)
        response.cache = "miss"
        response.cache_key = key
        await self._count("miss")
        return response

    async def discard(self, response: LLMResponse) -> None:
        """Remove a cached entry that turned out to be unusable (failed re-verification)."""
        if response.cache == "hit" and response.cache_key:
            await self._drop(response.cache_key)

    async def remember(self, response: LLMResponse) -> bool:
        """Store a response the caller has VALIDATED. Only fresh (miss) responses are stored."""
        s = self._settings
        if response.cache != "miss" or not response.cache_key or not s.llm_cache_enabled:
            return False
        payload = json.dumps(
            {
                "text": response.text,
                "model": response.model,
                "usage": [response.usage.prompt_tokens, response.usage.completion_tokens],
                "stored_at": int(time.time()),
            },
            ensure_ascii=False,
        )
        if len(payload.encode("utf-8")) > s.llm_cache_max_bytes:
            return False
        try:
            await self._redis_call(
                self._redis.set(response.cache_key, payload, ex=s.llm_cache_ttl_s)
            )
        except (RedisError, OSError, TimeoutError) as exc:
            log.warning("llm_cache_store_failed", error=type(exc).__name__)
            return False
        return True


def user_pattern(user_id: uuid.UUID) -> str:
    return f"{KEY_PREFIX}:{KEY_VERSION}:u:{subject_key(str(user_id))}:*"


async def purge_user_cache(redis: AsyncRedis, user_id: uuid.UUID) -> int:
    pattern = user_pattern(user_id)
    keys: list[bytes] = []
    async for key in redis.scan_iter(match=pattern, count=500):  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]
        keys.append(key)  # pyright: ignore[reportUnknownArgumentType]
    return int(await redis.delete(*keys)) if keys else 0


def purge_user_cache_sync(redis: Redis, user_id: uuid.UUID) -> int:  # pyright: ignore[reportMissingTypeArgument]
    pattern = user_pattern(user_id)
    keys: list[bytes] = []
    for key in redis.scan_iter(match=pattern, count=500):  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]
        keys.append(key)  # pyright: ignore[reportUnknownArgumentType]
    return int(redis.delete(*keys)) if keys else 0  # pyright: ignore[reportArgumentType]
