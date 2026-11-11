"""OpenAI-compatible chat-completions client (one class for every free hosted provider).

Gemini (`.../v1beta/openai/`), OpenRouter (`.../api/v1`, `:free` models), Hugging Face Inference
Providers (`https://router.huggingface.co/v1`), Groq (`.../openai/v1`) and many others expose
`POST {base_url}/chat/completions`. Free tiers throttle hard, so this client has:
  * a hard per-call timeout, a TOTAL deadline across every retry and fallback
    (`LLM_TOTAL_BUDGET_S`), a process-wide concurrency limit (`LLM_MAX_CONCURRENCY`) and a bounded
    wait for a free slot (`LLM_QUEUE_TIMEOUT_S`);
  * 429: free quotas are per model, so the next fallback model is tried at once (no retry storm
    against a throttled model); the smallest Retry-After seen is reported when all are throttled;
  * 5xx / timeouts / connection errors: bounded retries with exponential backoff + jitter on the
    same model, then the next model;
  * 401 / 403: the same key is used for every model, so the whole chain stops;
  * 404 (unknown or retired model): next model; 400: the request is downgraded one JSON mode
    (json_schema -> json_object -> prompt) once per model, then the next model;
  * stable, user-safe errors (never the provider's raw body, never the API key).
One instance per process (`Infrastructure.llm`) so the concurrency limit is process-wide.
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import Any

import httpx

from app.core.config import Settings
from app.core.exceptions import (
    ModelNotConfiguredError,
    ModelProviderError,
    ModelRateLimitedError,
    ModelTimeoutError,
)
from app.core.logging import get_logger
from app.llm.base import LLMRequest, LLMResponse, Usage
from app.llm.budget import QuotaGuard

log = get_logger(__name__)

_RETRY_SAME_MODEL = frozenset({408, 409, 425, 500, 502, 503, 504})
_MODE_DOWNGRADE = {"json_schema": "json_object", "json_object": "prompt"}


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after")
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None


class _Failure(Exception):
    """Internal: a failed attempt (kept apart from AppError so callers never see raw details)."""

    def __init__(self, kind: str, retry_after: float | None = None) -> None:
        super().__init__(kind)
        # rate_limited | timeout | unavailable | rejected | bad_request | auth | malformed
        self.kind = kind
        self.retry_after = retry_after


class OpenAICompatibleModel:
    def __init__(
        self,
        settings: Settings,
        client: httpx.AsyncClient | None = None,
        *,
        sleep: Any = asyncio.sleep,
        quota: QuotaGuard | None = None,
    ) -> None:
        self._settings = settings
        self._quota = quota
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(settings.llm_timeout_s, connect=10.0)
        )
        self._sem = asyncio.Semaphore(settings.llm_max_concurrency)
        self._sleep = sleep
        # models whose provider rejected a stricter response_format: remembered per process
        self._mode: dict[str, str] = {}

    @property
    def model_name(self) -> str:
        return self._settings.llm_model

    @property
    def configured(self) -> bool:
        s = self._settings
        return bool(s.llm_base_url and s.llm_model and s.llm_api_key.get_secret_value())

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # ------------------------------------------------------------------ request building

    def _payload(self, request: LLMRequest, model: str) -> dict[str, Any]:
        s = self._settings
        payload: dict[str, Any] = {
            "model": model,
            "messages": [{"role": m.role, "content": m.content} for m in request.messages],
            "temperature": s.llm_temperature
            if request.temperature is None
            else request.temperature,
            "max_tokens": request.max_tokens or s.llm_max_tokens,
            "stream": False,
        }
        mode = self._mode.get(model, s.llm_json_mode)
        if request.json_schema is not None and mode == "json_schema":
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": request.schema_name,
                    "schema": request.json_schema,
                    "strict": False,
                },
            }
        elif request.json_schema is not None and mode == "json_object":
            payload["response_format"] = {"type": "json_object"}
        return payload

    def _headers(self) -> dict[str, str]:
        s = self._settings
        return {
            "Authorization": f"Bearer {s.llm_api_key.get_secret_value()}",
            "Content-Type": "application/json",
            **s.llm_extra_headers,
        }

    # ------------------------------------------------------------------ one attempt

    async def _attempt(self, request: LLMRequest, model: str) -> tuple[str, Usage]:
        url = self._settings.llm_base_url.rstrip("/") + "/chat/completions"
        try:
            response = await self._client.post(
                url, json=self._payload(request, model), headers=self._headers()
            )
        except httpx.TimeoutException as exc:
            raise _Failure("timeout") from exc
        except httpx.HTTPError as exc:
            raise _Failure("unavailable") from exc
        code = response.status_code
        if code == 429:
            raise _Failure("rate_limited", _retry_after(response))
        if code in (401, 403):
            log.warning("llm_auth_rejected", status=code, model=model)
            raise _Failure("auth")
        if code == 404:
            raise _Failure("rejected")
        if code in _RETRY_SAME_MODEL:
            raise _Failure("unavailable", _retry_after(response))
        if code >= 400:
            log.warning("llm_request_rejected", status=code, model=model)
            raise _Failure("bad_request")
        try:
            body: dict[str, Any] = response.json()
            text = body["choices"][0]["message"]["content"]
            if not isinstance(text, str) or not text.strip():
                raise ValueError("empty completion")
            usage: dict[str, Any] = body.get("usage") or {}
            return text, Usage(
                int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)
            )
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise _Failure("malformed") from exc

    def _backoff(self, attempt: int, retry_after: float | None) -> float:
        s = self._settings
        delay = min(s.llm_retry_max_s, s.llm_retry_base_s * (2**attempt))
        delay += random.uniform(0, s.llm_retry_base_s / 2)  # noqa: S311 - jitter, not crypto
        if retry_after is not None:
            delay = max(delay, min(retry_after, s.llm_retry_max_s))
        return delay

    async def _guarded_attempt(self, request: LLMRequest, model: str) -> tuple[str, Usage]:
        if self._quota is not None:
            decision = await self._quota.reserve(model)
            if not decision.allowed:
                log.info("llm_quota_refused", model=model, reason=decision.reason)
                raise _Failure("rate_limited", float(decision.retry_after_s))
        try:
            async with asyncio.timeout(self._settings.llm_queue_timeout_s):
                await self._sem.acquire()
        except TimeoutError as exc:
            raise ModelRateLimitedError(5) from exc
        try:
            return await self._attempt(request, model)
        finally:
            self._sem.release()

    # ------------------------------------------------------------------ public

    async def complete(self, request: LLMRequest) -> LLMResponse:
        s = self._settings
        if not self.configured:
            raise ModelNotConfiguredError()
        models = [s.llm_model, *[m for m in s.llm_fallback_models if m != s.llm_model]]
        started = time.perf_counter()
        attempts = 0
        throttled: list[float] = []
        last: _Failure | None = None
        for index, model in enumerate(models):
            retry = 0
            while True:
                if time.perf_counter() - started > s.llm_total_budget_s:
                    raise ModelTimeoutError()
                attempts += 1
                try:
                    text, usage = await self._guarded_attempt(request, model)
                except _Failure as failure:
                    last = failure
                    log.warning(
                        "llm_attempt_failed", kind=failure.kind, model=model, attempt=attempts
                    )
                    if failure.kind == "auth":
                        raise ModelProviderError() from failure  # same key for every model
                    if failure.kind == "rate_limited":
                        throttled.append(failure.retry_after or 30.0)
                        break  # free quotas are per model: next model at once
                    if failure.kind == "bad_request":
                        current = self._mode.get(model, s.llm_json_mode)
                        lower = _MODE_DOWNGRADE.get(current)
                        if request.json_schema is not None and lower is not None:
                            self._mode[model] = lower
                            log.info("llm_json_mode_downgraded", model=model, to=lower)
                            continue  # same model, simpler response_format, no backoff
                        break
                    if failure.kind in ("rejected", "malformed"):
                        break
                    # timeout / unavailable: bounded retries on the same model
                    too_slow = (
                        failure.retry_after is not None and failure.retry_after > s.llm_retry_max_s
                    )
                    if retry < s.llm_max_retries and not too_slow:
                        await self._sleep(self._backoff(retry, failure.retry_after))
                        retry += 1
                        continue
                    break
                return LLMResponse(
                    text=text,
                    model=model,
                    usage=usage,
                    latency_ms=(time.perf_counter() - started) * 1000,
                    attempts=attempts,
                    fallback_used=index > 0,
                )
        assert last is not None  # noqa: S101 - the loop ran at least once
        if throttled and last.kind == "rate_limited":
            raise ModelRateLimitedError(int(min(throttled)))
        if last.kind == "timeout":
            raise ModelTimeoutError()
        raise ModelProviderError()
