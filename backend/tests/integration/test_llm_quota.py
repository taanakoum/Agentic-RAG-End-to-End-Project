"""The free-tier quota guard against a real Redis, and its integration with the provider."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import httpx2
import pytest
from pydantic import SecretStr
from redis.exceptions import ConnectionError as RedisConnectionError

from app.core.config import Settings, get_settings
from app.core.exceptions import ModelRateLimitedError
from app.infra.container import Infrastructure
from app.llm.base import ChatMessage, LLMRequest
from app.llm.budget import QuotaGuard, day_key, seconds_until_next_day
from app.llm.openai_compat import OpenAICompatibleModel
from tests.helpers import delete_keys

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
async def _clean(infra: Infrastructure) -> None:
    await delete_keys(infra.redis, "llmq:*")


def make_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "llm_base_url": "https://llm.example.test/v1",
        "llm_api_key": SecretStr("test-key"),
        "llm_model": "primary",
        "llm_retry_base_s": 0.0,
    }
    base.update(overrides)
    return get_settings().model_copy(update=base)


async def test_the_daily_budget_is_enforced_per_model_and_refusals_are_not_counted(
    infra: Infrastructure,
) -> None:
    guard = QuotaGuard(infra.redis, make_settings(llm_daily_request_budget=3))
    assert [(await guard.reserve("m1")).allowed for _ in range(3)] == [True, True, True]
    refused = await guard.reserve("m1")
    assert not refused.allowed and refused.reason == "daily_budget" and refused.retry_after_s >= 1
    assert (await guard.reserve("m2")).allowed  # another model has its own quota
    (usage,) = await guard.usage(["m1"])
    assert (usage.used_today, usage.daily_budget, usage.remaining) == (
        3,
        3,
        0,
    )  # refusal not counted
    for _ in range(3):
        assert not (await guard.reserve("m1")).allowed
    assert (await guard.usage(["m1"]))[0].used_today == 3


async def test_the_per_minute_limit_refuses_and_gives_the_day_slot_back(
    infra: Infrastructure,
) -> None:
    guard = QuotaGuard(
        infra.redis, make_settings(llm_requests_per_minute=2, llm_daily_request_budget=100)
    )
    assert (await guard.reserve("m")).allowed and (await guard.reserve("m")).allowed
    third = await guard.reserve("m")
    assert not third.allowed and third.reason == "per_minute" and 1 <= third.retry_after_s <= 60
    assert (await guard.usage(["m"]))[0].used_today == 2  # the refused attempt cost no daily slot


async def test_a_disabled_guard_never_touches_redis(infra: Infrastructure) -> None:
    guard = QuotaGuard(infra.redis, make_settings())
    assert not guard.enabled and (await guard.reserve("m")).allowed
    assert not [k async for k in infra.redis.scan_iter(match="llmq:*")]  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]
    assert (await guard.usage(["m"]))[0].remaining is None  # unlimited


async def test_a_redis_outage_never_blocks_chat(infra: Infrastructure) -> None:
    class DownRedis:
        async def incr(self, *_a: Any, **_k: Any) -> Any:
            raise RedisConnectionError("down")

        async def get(self, *_a: Any, **_k: Any) -> Any:
            raise RedisConnectionError("down")

    guard = QuotaGuard(DownRedis(), make_settings(llm_daily_request_budget=1))  # pyright: ignore[reportArgumentType]
    assert (await guard.reserve("m")).allowed and (await guard.reserve("m")).allowed
    assert (await guard.usage(["m"]))[0].used_today == 0


async def test_concurrent_requests_never_exceed_the_budget(infra: Infrastructure) -> None:
    guard = QuotaGuard(infra.redis, make_settings(llm_daily_request_budget=10))
    decisions = await asyncio.gather(*(guard.reserve("m") for _ in range(40)))
    assert sum(d.allowed for d in decisions) == 10
    assert (await guard.usage(["m"]))[0].used_today == 10


def test_the_day_rolls_over_in_the_providers_timezone() -> None:
    la = ZoneInfo("America/Los_Angeles")
    late = datetime(2026, 11, 11, 23, 30, tzinfo=la)
    assert day_key(late) == "20261111" and seconds_until_next_day(late) == 30 * 60
    just_after = datetime(2026, 11, 12, 0, 0, 5, tzinfo=la)
    assert day_key(just_after) == "20261112" and seconds_until_next_day(just_after) > 86000
    utc_view = late.astimezone(ZoneInfo("UTC"))
    assert day_key(utc_view) == "20261112"  # the same instant is a different UTC day: tz matters


async def test_an_unknown_timezone_falls_back_to_utc_instead_of_failing(
    infra: Infrastructure,
) -> None:
    guard = QuotaGuard(
        infra.redis, make_settings(llm_daily_request_budget=1, llm_quota_timezone="Not/AZone")
    )
    assert (await guard.reserve("m")).allowed and not (await guard.reserve("m")).allowed


# ------------------------------------------------------------------ provider integration


def provider(infra: Infrastructure, handler: Any, **overrides: Any) -> OpenAICompatibleModel:
    settings = make_settings(**overrides)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OpenAICompatibleModel(settings, client, quota=QuotaGuard(infra.redis, settings))


def ok(text: str = "answer") -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})


REQUEST = LLMRequest(messages=(ChatMessage("user", "hi"),))


async def test_an_exhausted_model_is_skipped_locally_and_the_fallback_answers(
    infra: Infrastructure,
) -> None:
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body: dict[str, Any] = json.loads(request.content)
        sent.append(str(body["model"]))
        return ok()

    model = provider(infra, handler, llm_daily_request_budget=2, llm_fallback_models=["backup"])
    answers = [await model.complete(REQUEST) for _ in range(4)]
    assert [a.model for a in answers] == ["primary", "primary", "backup", "backup"]
    assert sent == ["primary", "primary", "backup", "backup"]  # no request was wasted on a refusal
    with pytest.raises(ModelRateLimitedError):  # both models are now out of budget
        await model.complete(REQUEST)
    assert len(sent) == 4


async def test_when_every_model_is_out_of_budget_the_user_gets_a_clean_429_with_the_real_wait(
    infra: Infrastructure,
) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return ok()

    model = provider(infra, handler, llm_daily_request_budget=1)
    await model.complete(REQUEST)
    with pytest.raises(ModelRateLimitedError) as exc:
        await model.complete(REQUEST)
    assert calls == 1  # the provider was not called again
    assert 1 <= exc.value.retry_after_s <= 86400


async def test_refused_attempts_do_not_burn_the_providers_real_quota(infra: Infrastructure) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return ok()

    model = provider(infra, handler, llm_requests_per_minute=2)
    results = await asyncio.gather(
        *(model.complete(REQUEST) for _ in range(6)), return_exceptions=True
    )
    assert sum(not isinstance(r, Exception) for r in results) == 2 and calls == 2


# ------------------------------------------------------------------ health endpoint and chat limits


async def test_the_llm_health_endpoint_reports_budget_without_secrets(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    resp = await client.get("/api/v1/health/llm")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {
        "configured",
        "daily_budget",
        "remaining_today",
        "resets_in_s",
        "per_minute_limit",
    }
    assert body["configured"] is False and body["remaining_today"] is None  # tests blank LLM_*
    assert "key" not in resp.text.lower() and "primary" not in resp.text


async def test_a_per_user_daily_chat_limit_protects_the_shared_quota(
    client_with_settings: Any,
) -> None:
    from tests.integration.agent_helpers import make_actor, make_session
    from tests.integration.docs_helpers import API

    settings = get_settings().model_copy(update={"rl_chat_daily_limit": 2})
    limited: httpx2.AsyncClient = await client_with_settings(settings)
    alice = await make_actor(limited)
    session = await make_session(limited, alice)
    codes: list[int] = []
    for n in range(4):
        resp = await limited.post(
            f"{API}/chat",
            json={"session_id": str(session), "message": "hello"},
            headers={**alice.headers, "Idempotency-Key": f"daily-limit-{n:05d}"},
        )
        codes.append(resp.status_code)
    assert codes == [200, 200, 429, 429]
    bob = await make_actor(limited, "bob@example.com")
    ok_resp = await limited.post(
        f"{API}/chat",
        json={"session_id": str(await make_session(limited, bob)), "message": "hello"},
        headers={**bob.headers, "Idempotency-Key": "daily-limit-bob01"},
    )
    assert ok_resp.status_code == 200  # limits are per user
