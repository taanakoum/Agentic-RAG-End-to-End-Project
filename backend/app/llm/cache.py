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

import contextlib
import hashlib
import json
import time
import uuid
from typing import Any

from redis import Redis
from redis.asyncio import Redis as AsyncRedis
from redis.exceptions import RedisError

from app.core.config import Settings
from app.core.logging import get_logger
from app.llm.base import ChatModel, LLMRequest, LLMResponse, Usage

log = get_logger(__name__)

KEY_VERSION = "v1"
_STATS_KEY = "llm:stats"


def namespace(request: LLMRequest) -> str:
    if request.cache_scope == "global":
        return "g"
    if request.user_id is None:
        raise ValueError("a user-scoped prompt needs a user_id")
    return f"u:{request.user_id}"


def cache_key(request: LLMRequest, settings: Settings) -> str:
    material: dict[str, Any] = {
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
    return f"llm:{KEY_VERSION}:{namespace(request)}:{digest}"


class LLMGateway:
    """The only way application code calls a model: cache lookup -> model -> (later) remember."""

    def __init__(self, model: ChatModel, redis: AsyncRedis, settings: Settings) -> None:
        self._model = model
        self._redis = redis
        self._settings = settings

    @property
    def model_name(self) -> str:
        return self._model.model_name

    async def _count(self, outcome: str) -> None:
        # statistics are best effort
        with contextlib.suppress(RedisError, OSError, TimeoutError):
            await self._redis.hincrby(_STATS_KEY, outcome, 1)  # pyright: ignore[reportGeneralTypeIssues]

    async def stats(self) -> dict[str, int]:
        try:
            raw: dict[Any, Any] = await self._redis.hgetall(_STATS_KEY)  # pyright: ignore[reportGeneralTypeIssues]
        except (RedisError, OSError, TimeoutError):
            return {}
        return {(k.decode() if isinstance(k, bytes) else str(k)): int(v) for k, v in raw.items()}

    async def complete(self, request: LLMRequest) -> LLMResponse:
        s = self._settings
        if not (s.llm_cache_enabled and request.use_cache):
            response = await self._model.complete(request)
            response.cache = "bypass"
            await self._count("bypass")
            return response
        key = cache_key(request, s)
        try:
            raw = await self._redis.get(key)
        except (RedisError, OSError, TimeoutError) as exc:
            log.warning("llm_cache_unavailable", error=type(exc).__name__)
            response = await self._model.complete(request)
            response.cache = "error"
            await self._count("error")
            return response
        if raw is not None:
            try:
                data = json.loads(raw)
                usage = data.get("usage") or [0, 0]
                await self._count("hit")
                return LLMResponse(
                    text=str(data["text"]),
                    model=str(data.get("model", self._model.model_name)),
                    usage=Usage(int(usage[0]), int(usage[1])),
                    cache="hit",
                    cache_key=key,
                )
            except (ValueError, KeyError, TypeError, IndexError):
                log.warning("llm_cache_entry_unreadable")  # fall through: treat as a miss
        response = await self._model.complete(request)
        response.cache = "miss"
        response.cache_key = key
        await self._count("miss")
        return response

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
            await self._redis.set(response.cache_key, payload, ex=s.llm_cache_ttl_s)
        except (RedisError, OSError, TimeoutError) as exc:
            log.warning("llm_cache_store_failed", error=type(exc).__name__)
            return False
        return True


def user_pattern(user_id: uuid.UUID) -> str:
    return f"llm:{KEY_VERSION}:u:{user_id}:*"


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
