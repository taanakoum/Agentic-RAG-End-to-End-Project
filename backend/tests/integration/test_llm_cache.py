"""The Redis LLM cache against a real Redis (the model itself is the scripted test double)."""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from app.core.config import Settings, get_settings
from app.infra.container import Infrastructure
from app.infra.rate_limit import subject_key
from app.llm.base import ChatMessage, LLMRequest
from app.llm.cache import (
    LLMGateway,
    cache_key,
    purge_user_cache,
    purge_user_cache_sync,
)
from tests.fakes import ScriptedModel
from tests.helpers import delete_keys

pytestmark = pytest.mark.integration

USER_A, USER_B = uuid.uuid4(), uuid.uuid4()


@pytest.fixture(autouse=True)
async def _clean_cache(infra: Infrastructure) -> None:
    await delete_keys(infra.redis, "llmc:*")


async def keys_matching(infra: Infrastructure, pattern: str) -> set[str]:
    found: set[str] = set()
    async for key in infra.redis.scan_iter(match=pattern):  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]
        found.add(key.decode() if isinstance(key, bytes) else str(key))  # pyright: ignore[reportUnknownArgumentType]
    return found


def make_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "llm_base_url": "https://llm.example.test/v1",
        "llm_model": "primary",
        "llm_cache_ttl_s": 120,
    }
    base.update(overrides)
    return get_settings().model_copy(update=base)


def req(question: str = "What is the budget?", **kwargs: Any) -> LLMRequest:
    kwargs.setdefault("user_id", USER_A)
    return LLMRequest(
        messages=(
            ChatMessage("system", "Answer from the context."),
            ChatMessage("user", f"CONTEXT: budget is 5.\nQUESTION: {question}"),
        ),
        json_schema={"type": "object"},
        schema_name="rag_answer",
        schema_version="1",
        **kwargs,
    )


def gateway(infra: Infrastructure, model: ScriptedModel, **overrides: Any) -> LLMGateway:
    return LLMGateway(model, infra.redis, make_settings(**overrides))


async def test_a_validated_response_is_served_from_the_cache_next_time(
    infra: Infrastructure,
) -> None:
    model = ScriptedModel('{"answer": "5"}')
    gw = gateway(infra, model)
    first = await gw.complete(req())
    assert first.cache == "miss" and first.cache_key and len(model.calls) == 1
    assert await gw.remember(first) is True
    second = await gw.complete(req())
    assert second.cache == "hit" and second.text == '{"answer": "5"}'
    assert (second.usage.prompt_tokens, second.usage.completion_tokens) == (10, 5)
    assert len(model.calls) == 1  # the hosted model was not called again
    third = await gw.complete(req())
    assert third.cache == "hit" and len(model.calls) == 1


async def test_nothing_is_cached_until_the_caller_has_validated_the_output(
    infra: Infrastructure,
) -> None:
    model = ScriptedModel("not valid json at all")
    gw = gateway(infra, model)
    first = await gw.complete(req())
    # the caller rejects it (never calls remember): the next identical call must go to the model
    second = await gw.complete(req())
    assert first.cache == second.cache == "miss" and len(model.calls) == 2


async def test_a_hit_cannot_be_stored_again_and_bypassed_calls_are_never_stored(
    infra: Infrastructure,
) -> None:
    model = ScriptedModel("answer")
    gw = gateway(infra, model)
    miss = await gw.complete(req())
    await gw.remember(miss)
    hit = await gw.complete(req())
    assert await gw.remember(hit) is False  # nothing to store: it came from the cache
    bypassed = await gw.complete(req("another question", use_cache=False))
    assert bypassed.cache == "bypass" and await gw.remember(bypassed) is False
    assert await gw.complete(req("another question")) is not None
    assert len(model.calls) == 3


async def test_the_key_depends_on_everything_that_shapes_the_answer(infra: Infrastructure) -> None:
    s = make_settings()
    base = cache_key(req(), s)
    assert base == cache_key(req(), s)  # stable
    variants = {
        "question": cache_key(req("A different question?"), s),
        "temperature": cache_key(req(temperature=0.9), s),
        "max_tokens": cache_key(req(max_tokens=77), s),
        "schema_version": cache_key(
            LLMRequest(
                messages=req().messages,
                json_schema={"type": "object"},
                schema_name="rag_answer",
                schema_version="2",
                user_id=USER_A,
            ),
            s,
        ),
        "schema": cache_key(
            LLMRequest(
                messages=req().messages,
                json_schema={"type": "array"},
                schema_name="rag_answer",
                user_id=USER_A,
            ),
            s,
        ),
        "model": cache_key(req(), make_settings(llm_model="other")),
        "fallbacks": cache_key(req(), make_settings(llm_fallback_models=["b"])),
        "host": cache_key(req(), make_settings(llm_base_url="https://other.test/v1")),
        "json_mode": cache_key(req(), make_settings(llm_json_mode="json_schema")),
        "context": cache_key(
            LLMRequest(
                messages=(
                    req().messages[0],
                    ChatMessage("user", "CONTEXT: budget is 6.\nQUESTION: What is the budget?"),
                ),
                json_schema={"type": "object"},
                schema_name="rag_answer",
                user_id=USER_A,
            ),
            s,
        ),
    }
    assert base not in variants.values() and len(set(variants.values())) == len(variants)


async def test_changed_context_means_no_stale_answer(infra: Infrastructure) -> None:
    """A document edit changes the retrieved text in the prompt, hence the key."""
    model = ScriptedModel("the budget is 5", "the budget is 6")
    gw = gateway(infra, model)

    def with_context(value: str) -> LLMRequest:
        return LLMRequest(
            messages=(ChatMessage("user", f"CONTEXT: budget is {value}\nQUESTION: budget?"),),
            user_id=USER_A,
        )

    old = await gw.complete(with_context("5"))
    await gw.remember(old)
    new = await gw.complete(with_context("6"))
    assert new.cache == "miss" and new.text == "the budget is 6"


# ------------------------------------------------------------------ tenant isolation


async def test_user_scoped_entries_are_never_shared_between_users(infra: Infrastructure) -> None:
    model = ScriptedModel("answer for a user")
    gw = gateway(infra, model)
    mine = await gw.complete(req(user_id=USER_A))
    await gw.remember(mine)
    other = await gw.complete(req(user_id=USER_B))  # identical prompt, different user
    assert other.cache == "miss" and len(model.calls) == 2
    assert f":u:{subject_key(str(USER_A))}:" in (mine.cache_key or "")
    assert f":u:{subject_key(str(USER_B))}:" in (other.cache_key or "")
    assert str(USER_A) not in (mine.cache_key or "")  # no raw user id in Redis keys
    assert (await gw.complete(req(user_id=USER_A))).cache == "hit"


async def test_only_context_free_prompts_can_be_shared_globally(infra: Infrastructure) -> None:
    model = ScriptedModel("Hello! How can I help?")
    gw = gateway(infra, model)
    greeting = LLMRequest(messages=(ChatMessage("user", "hello"),), cache_scope="global")
    first = await gw.complete(greeting)
    await gw.remember(first)
    again = await gw.complete(
        LLMRequest(messages=(ChatMessage("user", "hello"),), cache_scope="global", user_id=USER_B)
    )
    assert again.cache == "hit" and (first.cache_key or "").startswith("llmc:v1:g:")
    assert len(model.calls) == 1


def test_a_user_scoped_prompt_without_a_user_is_refused() -> None:
    with pytest.raises(ValueError):
        cache_key(LLMRequest(messages=(ChatMessage("user", "x"),), user_id=None), make_settings())


async def test_purging_a_user_removes_only_that_users_entries(infra: Infrastructure) -> None:
    model = ScriptedModel("x")
    gw = gateway(infra, model)
    for user in (USER_A, USER_B):
        for q in ("one", "two", "three"):
            await gw.remember(await gw.complete(req(q, user_id=user)))
    greeting = LLMRequest(messages=(ChatMessage("user", "hi"),), cache_scope="global")
    await gw.remember(await gw.complete(greeting))
    assert await purge_user_cache(infra.redis, USER_A) == 3
    remaining = await keys_matching(infra, "llmc:v1:*")
    assert len(remaining) == 4 and not any(subject_key(str(USER_A)) in k for k in remaining)
    with infra.sync_session_factory():  # the sync purge used by the worker
        from app.infra.clients import create_sync_redis

        sync = create_sync_redis(get_settings())
        try:
            assert purge_user_cache_sync(sync, USER_B) == 3
        finally:
            sync.close()
    assert await purge_user_cache(infra.redis, USER_A) == 0  # idempotent


# ------------------------------------------------------------------ limits, TTL, failure modes


async def test_entries_expire_after_the_configured_ttl(infra: Infrastructure) -> None:
    gw = gateway(infra, ScriptedModel("x"), llm_cache_ttl_s=90)
    first = await gw.complete(req())
    await gw.remember(first)
    ttl = await infra.redis.ttl(first.cache_key or "")
    assert 0 < ttl <= 90


async def test_oversized_outputs_are_not_cached(infra: Infrastructure) -> None:
    model = ScriptedModel("y" * 5000)
    gw = gateway(infra, model, llm_cache_max_bytes=1024)
    first = await gw.complete(req())
    assert await gw.remember(first) is False
    assert (await gw.complete(req())).cache == "miss" and len(model.calls) == 2


async def test_the_cache_can_be_switched_off(infra: Infrastructure) -> None:
    model = ScriptedModel("x")
    gw = gateway(infra, model, llm_cache_enabled=False)
    first = await gw.complete(req())
    assert first.cache == "bypass" and await gw.remember(first) is False
    assert (await gw.complete(req())).cache == "bypass" and len(model.calls) == 2
    assert not await keys_matching(infra, "llmc:v1:*")


async def test_a_redis_outage_is_a_miss_not_a_failed_chat(infra: Infrastructure) -> None:
    class DownRedis:
        async def get(self, *_a: Any, **_k: Any) -> Any:
            raise RedisConnectionError("redis is down")

        async def set(self, *_a: Any, **_k: Any) -> Any:
            raise RedisConnectionError("redis is down")

        async def hincrby(self, *_a: Any, **_k: Any) -> Any:
            raise RedisConnectionError("redis is down")

        async def hgetall(self, *_a: Any, **_k: Any) -> Any:
            raise RedisConnectionError("redis is down")

    model = ScriptedModel("still answers")
    gw = LLMGateway(model, DownRedis(), make_settings())  # pyright: ignore[reportArgumentType]
    response = await gw.complete(req())
    assert response.text == "still answers" and response.cache == "error"
    assert await gw.remember(response) is False  # and storing fails quietly too
    assert await gw.stats() == {}


async def test_provider_errors_are_not_cached_and_propagate(infra: Infrastructure) -> None:
    from app.core.exceptions import ModelProviderError

    model = ScriptedModel(ModelProviderError(), "recovered")
    gw = gateway(infra, model)
    with pytest.raises(ModelProviderError):
        await gw.complete(req())
    recovered = await gw.complete(req())
    assert recovered.text == "recovered" and recovered.cache == "miss"


async def test_an_unreadable_cache_entry_is_treated_as_a_miss(infra: Infrastructure) -> None:
    model = ScriptedModel("fresh")
    gw = gateway(infra, model)
    key = cache_key(req(), make_settings())
    await infra.redis.set(key, "{this is not json", ex=60)
    response = await gw.complete(req())
    assert response.text == "fresh" and response.cache == "miss"
    await infra.redis.set(key, json.dumps({"no_text_field": True}), ex=60)
    assert (await gw.complete(req())).cache == "miss"


async def test_hit_and_miss_counters_are_kept(infra: Infrastructure) -> None:
    gw = gateway(infra, ScriptedModel("x"))
    await infra.redis.delete("llmc:stats")
    miss = await gw.complete(req())
    await gw.remember(miss)
    await gw.complete(req())
    await gw.complete(req())
    await gw.complete(req("uncached", use_cache=False))
    assert await gw.stats() == {"miss": 1, "hit": 2, "bypass": 1}


async def test_concurrent_identical_requests_all_get_a_valid_answer(infra: Infrastructure) -> None:
    """No stampede protection is promised (free quota matters, correctness matters more)."""
    model = ScriptedModel("same answer")
    gw = gateway(infra, model)
    results = await asyncio.gather(*(gw.complete(req()) for _ in range(8)))
    assert {r.text for r in results} == {"same answer"}
    await asyncio.gather(*(gw.remember(r) for r in results))
    assert (await gw.complete(req())).cache == "hit"


async def test_a_broken_entry_is_deleted_and_a_failed_hit_can_be_discarded(
    infra: Infrastructure,
) -> None:
    model = ScriptedModel("fresh")
    gw = gateway(infra, model)
    key = cache_key(req(), make_settings())
    await infra.redis.set(key, "{broken", ex=60)
    first = await gw.complete(req())
    assert first.cache == "miss" and await infra.redis.exists(key) == 0  # dropped on read
    await gw.remember(first)
    hit = await gw.complete(req())
    assert hit.cache == "hit" and await infra.redis.exists(key) == 1
    await gw.discard(hit)  # the agent found the cached answer unusable (citation check failed)
    assert await infra.redis.exists(key) == 0
    assert (await gw.complete(req())).cache == "miss"
    miss = await gw.complete(req("something else"))
    await gw.discard(miss)  # only hits are discarded: a miss has no entry
    assert miss.cache == "miss"


async def test_a_slow_redis_is_a_miss_within_the_cache_timeout(infra: Infrastructure) -> None:
    class SlowRedis:
        async def get(self, *_a: Any, **_k: Any) -> Any:
            await asyncio.sleep(2)

        async def hincrby(self, *_a: Any, **_k: Any) -> Any:
            await asyncio.sleep(2)

    model = ScriptedModel("answer")
    gw = LLMGateway(model, SlowRedis(), make_settings(llm_cache_timeout_s=0.05))  # pyright: ignore[reportArgumentType]
    started = asyncio.get_running_loop().time()
    response = await gw.complete(req())
    assert response.text == "answer" and response.cache == "error"
    assert asyncio.get_running_loop().time() - started < 1.0  # not the 2 s a slow Redis needs


async def test_one_user_cannot_occupy_more_than_their_share_of_model_calls(
    infra: Infrastructure,
) -> None:
    active = 0
    peak = 0

    async def slow(_request: Any) -> str:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.05)
        active -= 1
        return "ok"

    class SlowModel(ScriptedModel):
        async def complete(self, request: Any) -> Any:
            from app.llm.base import LLMResponse

            return LLMResponse(text=await slow(request), model="m")

    gw = gateway(infra, SlowModel("x"), llm_max_concurrency_per_user=2, llm_cache_enabled=False)
    await asyncio.gather(*(gw.complete(req(f"q{i}", user_id=USER_A)) for i in range(8)))
    assert peak == 2
    other_peak_before = peak
    # a second user is not blocked by the first one's queue
    await asyncio.gather(
        *(gw.complete(req(f"q{i}", user_id=USER_A)) for i in range(4)),
        *(gw.complete(req(f"r{i}", user_id=USER_B)) for i in range(4)),
    )
    assert peak <= 4 and other_peak_before == 2


async def test_waiting_for_a_per_user_slot_is_bounded(infra: Infrastructure) -> None:
    from app.core.exceptions import ModelRateLimitedError

    model = ScriptedModel("x")
    model.delay_s = 0.3
    gw = gateway(
        infra,
        model,
        llm_max_concurrency_per_user=1,
        llm_queue_timeout_s=0.05,
        llm_cache_enabled=False,
    )
    first = asyncio.create_task(gw.complete(req("one")))
    await asyncio.sleep(0.02)
    with pytest.raises(ModelRateLimitedError):
        await gw.complete(req("two"))
    assert (await first).text == "x"
