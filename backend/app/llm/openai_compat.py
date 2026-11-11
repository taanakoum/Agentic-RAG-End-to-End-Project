"""OpenAI-compatible chat-completions client (one class for every free hosted provider).

Gemini (`.../v1beta/openai/`), OpenRouter (`.../api/v1`, `:free` models), Hugging Face Inference
Providers (`https://router.huggingface.co/v1`), Groq (`.../openai/v1`) and many others expose
`POST {base_url}/chat/completions`. Free tiers throttle hard, so this client has:
  * a hard per-call timeout and a process-wide concurrency limit (`LLM_MAX_CONCURRENCY`);
  * bounded retries with exponential backoff + jitter on 429 / 5xx / timeouts, honouring
    `Retry-After`;
  * an ordered fallback chain (`LLM_FALLBACK_MODELS`) used when a model is rate limited, down or
    unknown; and
  * stable, user-safe errors (never the provider's raw body, never the API key).
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

log = get_logger(__name__)

_RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})
_FALLBACK_STATUS = frozenset({404})  # unknown / retired model: try the next one


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after")
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None


class _ProviderFailure(Exception):
    """Internal: a failed attempt (kept apart from AppError so callers never see raw details)."""

    def __init__(self, kind: str, retry_after: float | None = None) -> None:
        super().__init__(kind)
        self.kind = kind  # rate_limited | timeout | unavailable | rejected | malformed
        self.retry_after = retry_after


class OpenAICompatibleModel:
    def __init__(
        self,
        settings: Settings,
        client: httpx.AsyncClient | None = None,
        *,
        sleep: Any = asyncio.sleep,
    ) -> None:
        self._settings = settings
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(settings.llm_timeout_s, connect=10.0)
        )
        self._sem = asyncio.Semaphore(settings.llm_max_concurrency)
        self._sleep = sleep

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
        if request.json_schema is not None and s.llm_json_mode == "json_schema":
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": request.schema_name,
                    "schema": request.json_schema,
                    "strict": False,
                },
            }
        elif request.json_schema is not None and s.llm_json_mode == "json_object":
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
            raise _ProviderFailure("timeout") from exc
        except httpx.HTTPError as exc:
            raise _ProviderFailure("unavailable") from exc
        if response.status_code == 429:
            raise _ProviderFailure("rate_limited", _retry_after(response))
        if response.status_code in _FALLBACK_STATUS:
            raise _ProviderFailure("rejected")
        if response.status_code in _RETRYABLE_STATUS:
            raise _ProviderFailure("unavailable", _retry_after(response))
        if response.status_code >= 400:
            # 400/401/403 ...: retrying cannot help (bad key, bad request, content policy)
            log.warning("llm_request_rejected", status=response.status_code, model=model)
            raise _ProviderFailure("rejected")
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
            raise _ProviderFailure("malformed") from exc

    def _backoff(self, attempt: int, retry_after: float | None) -> float:
        s = self._settings
        delay = min(s.llm_retry_max_s, s.llm_retry_base_s * (2**attempt))
        delay += random.uniform(0, s.llm_retry_base_s / 2)  # noqa: S311 - jitter, not crypto
        if retry_after is not None:
            delay = max(delay, min(retry_after, s.llm_retry_max_s))
        return delay

    # ------------------------------------------------------------------ public

    async def complete(self, request: LLMRequest) -> LLMResponse:
        s = self._settings
        if not self.configured:
            raise ModelNotConfiguredError()
        models = [s.llm_model, *[m for m in s.llm_fallback_models if m != s.llm_model]]
        started = time.perf_counter()
        attempts = 0
        last: _ProviderFailure | None = None
        for index, model in enumerate(models):
            for retry in range(s.llm_max_retries + 1):
                attempts += 1
                try:
                    async with self._sem:
                        text, usage = await self._attempt(request, model)
                except _ProviderFailure as failure:
                    last = failure
                    log.warning(
                        "llm_attempt_failed", kind=failure.kind, model=model, attempt=attempts
                    )
                    if failure.kind in ("rejected", "malformed"):
                        break  # no point retrying this model; try the next one
                    if retry < s.llm_max_retries:
                        await self._sleep(self._backoff(retry, failure.retry_after))
                        continue
                    break  # retries exhausted for this model
                return LLMResponse(
                    text=text,
                    model=model,
                    usage=usage,
                    latency_ms=(time.perf_counter() - started) * 1000,
                    attempts=attempts,
                    fallback_used=index > 0,
                )
        assert last is not None  # noqa: S101 - the loop ran at least once
        if last.kind == "rate_limited":
            raise ModelRateLimitedError(int(last.retry_after or 30))
        if last.kind == "timeout":
            raise ModelTimeoutError()
        raise ModelProviderError()
