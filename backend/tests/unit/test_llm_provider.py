"""The OpenAI-compatible client against a fake HTTP transport (no network, no hosted model)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.core.exceptions import (
    ModelNotConfiguredError,
    ModelProviderError,
    ModelRateLimitedError,
    ModelTimeoutError,
)
from app.llm.base import ChatMessage, LLMRequest
from app.llm.openai_compat import OpenAICompatibleModel

SECRET = "sk-test-super-secret-key-123456"
Handler = Callable[[httpx.Request], httpx.Response]


def ok(text: str = '{"answer": "hi"}', usage: tuple[int, int] = (11, 7)) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"role": "assistant", "content": text}}],
            "usage": {"prompt_tokens": usage[0], "completion_tokens": usage[1]},
        },
    )


def settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "llm_base_url": "https://llm.example.test/v1/",
        "llm_api_key": SecretStr(SECRET),
        "llm_model": "primary-model",
        "llm_retry_base_s": 0.01,
        "llm_retry_max_s": 0.05,
        "llm_max_retries": 2,
    }
    base.update(overrides)
    return Settings.build(env_file=None, **base)


class Harness:
    def __init__(self, handler: Handler, **overrides: Any) -> None:
        self.requests: list[httpx.Request] = []
        self.sleeps: list[float] = []

        def recording(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return handler(request)

        async def fake_sleep(seconds: float) -> None:
            self.sleeps.append(seconds)

        client = httpx.AsyncClient(transport=httpx.MockTransport(recording))
        self.model = OpenAICompatibleModel(settings(**overrides), client, sleep=fake_sleep)

    def bodies(self) -> list[dict[str, Any]]:
        return [json.loads(r.content) for r in self.requests]


def request(**kwargs: Any) -> LLMRequest:
    return LLMRequest(
        messages=(ChatMessage("system", "Be brief."), ChatMessage("user", "Say hi.")), **kwargs
    )


async def test_success_parses_text_usage_and_sends_the_expected_request() -> None:
    h = Harness(lambda _r: ok('{"answer": "hello"}', (21, 9)), llm_extra_headers={"X-Title": "rag"})
    result = await h.model.complete(request(max_tokens=200, temperature=0.3))
    assert result.text == '{"answer": "hello"}' and result.model == "primary-model"
    assert (result.usage.prompt_tokens, result.usage.completion_tokens) == (21, 9)
    assert result.attempts == 1 and not result.fallback_used and result.latency_ms >= 0
    (sent,) = h.requests
    assert str(sent.url) == "https://llm.example.test/v1/chat/completions"
    assert sent.headers["authorization"] == f"Bearer {SECRET}" and sent.headers["x-title"] == "rag"
    body = h.bodies()[0]
    assert body["model"] == "primary-model" and body["stream"] is False
    assert body["messages"] == [
        {"role": "system", "content": "Be brief."},
        {"role": "user", "content": "Say hi."},
    ]
    assert body["max_tokens"] == 200 and body["temperature"] == 0.3
    assert "response_format" not in body  # no schema requested


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("json_object", {"type": "json_object"}),
        ("json_schema", "schema"),
        ("prompt", None),
    ],
)
async def test_the_json_mode_setting_controls_the_response_format(mode: str, expected: Any) -> None:
    h = Harness(lambda _r: ok(), llm_json_mode=mode)
    schema = {"type": "object", "properties": {"answer": {"type": "string"}}}
    await h.model.complete(request(json_schema=schema, schema_name="rag_answer"))
    sent: Any = h.bodies()[0].get("response_format")
    if expected == "schema":
        assert sent["type"] == "json_schema" and sent["json_schema"]["schema"] == schema
        assert sent["json_schema"]["name"] == "rag_answer"
    else:
        assert sent == expected


async def test_defaults_come_from_the_settings() -> None:
    h = Harness(lambda _r: ok(), llm_max_tokens=321, llm_temperature=0.55)
    await h.model.complete(request())
    assert h.bodies()[0]["max_tokens"] == 321 and h.bodies()[0]["temperature"] == 0.55


# ------------------------------------------------------------------ retries


async def test_server_errors_with_retry_after_are_retried_after_at_least_that_long() -> None:
    calls = iter([503, 503, 200])

    def handler(_r: httpx.Request) -> httpx.Response:
        status = next(calls)
        if status == 503:
            return httpx.Response(503, headers={"Retry-After": "0.04"}, text="overloaded")
        return ok()

    h = Harness(handler)
    result = await h.model.complete(request())
    assert result.attempts == 3 and len(h.requests) == 3
    assert len(h.sleeps) == 2 and all(s >= 0.04 for s in h.sleeps)
    assert all(s <= 0.05 + 0.01 for s in h.sleeps)  # bounded by LLM_RETRY_MAX_S (+ jitter)


async def test_a_retry_after_beyond_the_cap_skips_the_pointless_retries() -> None:
    h = Harness(
        lambda _r: httpx.Response(503, headers={"Retry-After": "600"}),
        llm_retry_max_s=0.05,
    )
    with pytest.raises(ModelProviderError):
        await h.model.complete(request())
    assert len(h.requests) == 1 and h.sleeps == []


async def test_rate_limits_are_not_retried_against_the_same_model() -> None:
    h = Harness(lambda _r: httpx.Response(429, headers={"Retry-After": "9"}, text="quota"))
    with pytest.raises(ModelRateLimitedError) as exc:
        await h.model.complete(request())
    assert len(h.requests) == 1 and h.sleeps == []  # no retry storm on a throttled model
    assert exc.value.headers["Retry-After"] == "9" and "quota" not in str(exc.value)


async def test_server_errors_are_retried_then_succeed() -> None:
    calls = iter([503, 500, 200])
    h = Harness(lambda _r: ok() if (s := next(calls)) == 200 else httpx.Response(s))
    assert (await h.model.complete(request())).attempts == 3


async def test_when_every_model_is_throttled_the_smallest_retry_after_is_reported() -> None:
    waits = {"primary-model": "30", "b1": "17", "b2": "45"}

    def handler(r: httpx.Request) -> httpx.Response:
        model = json.loads(r.content)["model"]
        return httpx.Response(429, headers={"Retry-After": waits[model]}, text="secret detail")

    h = Harness(handler, llm_fallback_models=["b1", "b2"])
    with pytest.raises(ModelRateLimitedError) as exc:
        await h.model.complete(request())
    assert exc.value.status_code == 429 and exc.value.headers["Retry-After"] == "17"
    assert "secret detail" not in str(exc.value) and len(h.requests) == 3  # one per model


async def test_persistent_server_errors_become_a_502_without_provider_details() -> None:
    h = Harness(lambda _r: httpx.Response(503, text="upstream exploded: key=" + SECRET))
    with pytest.raises(ModelProviderError) as exc:
        await h.model.complete(request())
    assert exc.value.status_code == 502
    assert SECRET not in str(exc.value) and "exploded" not in str(exc.value)


async def test_timeouts_are_retried_then_reported_as_504() -> None:
    def handler(_r: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow")

    h = Harness(handler)
    with pytest.raises(ModelTimeoutError) as exc:
        await h.model.complete(request())
    assert exc.value.status_code == 504 and len(h.requests) == 3


async def test_connection_errors_are_provider_errors() -> None:
    def handler(_r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(ModelProviderError):
        await Harness(handler).model.complete(request())


@pytest.mark.parametrize("status", [400, 401, 403])
async def test_client_errors_are_not_retried(status: int) -> None:
    h = Harness(lambda _r: httpx.Response(status, text="bad key " + SECRET))
    with pytest.raises(ModelProviderError) as exc:
        await h.model.complete(request())
    assert len(h.requests) == 1 and h.sleeps == []  # no retry storm on a wrong key
    assert SECRET not in str(exc.value) and "bad key" not in str(exc.value)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={"choices": []}),
        httpx.Response(200, json={"choices": [{"message": {"content": ""}}]}),
        httpx.Response(200, json={"choices": [{"message": {"content": None}}]}),
        httpx.Response(200, json={"nope": 1}),
    ],
)
async def test_malformed_success_bodies_are_provider_errors(response: httpx.Response) -> None:
    with pytest.raises(ModelProviderError):
        await Harness(lambda _r: response).model.complete(request())


# ------------------------------------------------------------------ fallback chain


async def test_an_unknown_primary_model_falls_back_to_the_next() -> None:
    def handler(r: httpx.Request) -> httpx.Response:
        model = json.loads(r.content)["model"]
        return httpx.Response(404) if model == "primary-model" else ok("from fallback")

    h = Harness(handler, llm_fallback_models=["backup-1", "backup-2"])
    result = await h.model.complete(request())
    assert result.text == "from fallback" and result.model == "backup-1" and result.fallback_used
    assert [b["model"] for b in h.bodies()] == ["primary-model", "backup-1"]


async def test_a_throttled_primary_falls_back_to_the_next_model_at_once() -> None:
    def handler(r: httpx.Request) -> httpx.Response:
        model = json.loads(r.content)["model"]
        return httpx.Response(429) if model == "primary-model" else ok("ok from backup")

    h = Harness(handler, llm_fallback_models=["backup-1"])
    result = await h.model.complete(request())
    assert result.model == "backup-1" and result.attempts == 2 and h.sleeps == []
    assert [b["model"] for b in h.bodies()] == ["primary-model", "backup-1"]


async def test_a_rejected_key_stops_the_whole_chain() -> None:
    h = Harness(lambda _r: httpx.Response(401), llm_fallback_models=["b1", "b2"])
    with pytest.raises(ModelProviderError):
        await h.model.complete(request())
    assert len(h.requests) == 1  # the same key would fail for every model


async def test_a_bad_request_downgrades_the_json_mode_once_and_remembers_it() -> None:
    def handler(r: httpx.Request) -> httpx.Response:
        body = json.loads(r.content)
        fmt = body.get("response_format")
        if fmt is not None and fmt["type"] == "json_schema":
            return httpx.Response(400, text="response_format not supported")
        return ok('{"answer": "ok"}')

    h = Harness(handler, llm_json_mode="json_schema")
    schema = {"type": "object"}
    first = await h.model.complete(request(json_schema=schema))
    assert first.text == '{"answer": "ok"}'
    formats = [b.get("response_format", {}).get("type") for b in h.bodies()]
    assert formats == ["json_schema", "json_object"] and h.sleeps == []
    await h.model.complete(request(json_schema=schema))
    assert [b.get("response_format", {}).get("type") for b in h.bodies()][-1] == "json_object"
    assert len(h.requests) == 3  # the learned mode is used straight away next time


async def test_a_persistent_bad_request_ends_with_the_plain_prompt_mode_then_the_next_model() -> (
    None
):
    h = Harness(
        lambda _r: httpx.Response(400, text="nope"),
        llm_json_mode="json_schema",
        llm_fallback_models=["b1"],
    )
    with pytest.raises(ModelProviderError):
        await h.model.complete(request(json_schema={"type": "object"}))
    seen = [(b["model"], b.get("response_format", {}).get("type")) for b in h.bodies()]
    assert seen == [
        ("primary-model", "json_schema"),
        ("primary-model", "json_object"),
        ("primary-model", None),
        ("b1", "json_schema"),
        ("b1", "json_object"),
        ("b1", None),
    ]


async def test_the_total_budget_bounds_the_whole_call() -> None:
    async def slow_failure(_r: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.06)
        return httpx.Response(503)

    client = httpx.AsyncClient(transport=httpx.MockTransport(slow_failure))
    model = OpenAICompatibleModel(
        settings(llm_total_budget_s=0.05, llm_max_retries=5, llm_fallback_models=["b1", "b2"]),
        client,
    )
    with pytest.raises(ModelTimeoutError):
        await model.complete(request())


async def test_waiting_for_a_free_slot_is_bounded() -> None:
    gate = asyncio.Event()

    async def blocked(_r: httpx.Request) -> httpx.Response:
        await gate.wait()
        return ok()

    client = httpx.AsyncClient(transport=httpx.MockTransport(blocked))
    model = OpenAICompatibleModel(settings(llm_max_concurrency=1, llm_queue_timeout_s=0.05), client)
    first = asyncio.create_task(model.complete(request()))
    await asyncio.sleep(0.02)
    with pytest.raises(ModelRateLimitedError):  # the second caller gives up waiting
        await model.complete(request())
    gate.set()
    assert (await first).text


async def test_when_every_model_fails_the_last_failure_decides_the_error() -> None:
    h = Harness(lambda _r: httpx.Response(429), llm_fallback_models=["b1", "b2"])
    with pytest.raises(ModelRateLimitedError):
        await h.model.complete(request())
    assert {b["model"] for b in h.bodies()} == {"primary-model", "b1", "b2"}


async def test_the_primary_model_is_not_tried_twice_when_listed_as_a_fallback() -> None:
    h = Harness(lambda _r: httpx.Response(404), llm_fallback_models=["primary-model", "b1"])
    with pytest.raises(ModelProviderError):
        await h.model.complete(request())
    assert [b["model"] for b in h.bodies()] == ["primary-model", "b1"]


# ------------------------------------------------------------------ configuration and limits


@pytest.mark.parametrize(
    "overrides",
    [{"llm_base_url": ""}, {"llm_model": ""}, {"llm_api_key": SecretStr("")}],
)
async def test_an_unconfigured_provider_fails_fast_without_any_http_call(
    overrides: dict[str, Any],
) -> None:
    h = Harness(lambda _r: ok(), **overrides)
    assert not h.model.configured
    with pytest.raises(ModelNotConfiguredError) as exc:
        await h.model.complete(request())
    assert exc.value.status_code == 503 and h.requests == []


async def test_the_process_wide_concurrency_limit_is_respected() -> None:
    in_flight = 0
    peak = 0

    async def slow(_r: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.03)
        in_flight -= 1
        return ok()

    client = httpx.AsyncClient(transport=httpx.MockTransport(slow))
    model = OpenAICompatibleModel(settings(llm_max_concurrency=2), client)
    await asyncio.gather(*(model.complete(request()) for _ in range(8)))
    assert peak == 2


async def test_the_api_key_never_appears_in_logs_or_reprs() -> None:
    s = settings()
    assert SECRET not in repr(s) and SECRET not in str(s.model_dump())
    h = Harness(lambda _r: httpx.Response(500, text=SECRET))
    with pytest.raises(ModelProviderError) as exc:
        await h.model.complete(request())
    assert SECRET not in repr(exc.value)


def test_fallback_models_can_be_given_as_a_comma_separated_list() -> None:
    s = Settings.build(env_file=None, llm_fallback_models="a:free, b ,,c")  # pyright: ignore[reportArgumentType]
    assert s.llm_fallback_models == ["a:free", "b", "c"]
    assert Settings.build(env_file=None).llm_fallback_models == []
