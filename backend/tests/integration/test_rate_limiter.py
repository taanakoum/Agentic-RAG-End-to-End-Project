from __future__ import annotations

import asyncio
import uuid

import pytest

from app.core.exceptions import RateLimitExceededError
from app.infra.container import Infrastructure
from app.infra.rate_limit import RateLimiter

pytestmark = pytest.mark.integration


def subject() -> str:
    return uuid.uuid4().hex


async def test_hit_allows_up_to_limit_then_blocks(infra: Infrastructure) -> None:
    limiter, who = RateLimiter(infra.redis), subject()
    for _ in range(3):
        await limiter.hit("t_basic", who, limit=3, window_s=30)
    with pytest.raises(RateLimitExceededError) as exc:
        await limiter.hit("t_basic", who, limit=3, window_s=30)
    assert 1 <= exc.value.retry_after_s <= 30
    assert exc.value.headers["Retry-After"] == str(exc.value.retry_after_s)


async def test_counter_always_has_a_ttl(infra: Infrastructure) -> None:
    limiter, who = RateLimiter(infra.redis), subject()
    await limiter.hit("t_ttl", who, limit=5, window_s=45)
    ttl = await infra.redis.ttl(f"rl:t_ttl:{who}")
    assert 0 < ttl <= 45


async def test_concurrent_hits_are_counted_exactly(infra: Infrastructure) -> None:
    limiter, who = RateLimiter(infra.redis), subject()
    results = await asyncio.gather(
        *(limiter.hit("t_conc", who, limit=40, window_s=30) for _ in range(100)),
        return_exceptions=True,
    )
    blocked = [r for r in results if isinstance(r, RateLimitExceededError)]
    ok = [r for r in results if r is None]
    assert len(ok) == 40
    assert len(blocked) == 60
    assert int(await infra.redis.get(f"rl:t_conc:{who}") or 0) == 100


async def test_check_does_not_count_and_reset_clears(infra: Infrastructure) -> None:
    limiter, who = RateLimiter(infra.redis), subject()
    await limiter.check("t_check", who, limit=2)  # nothing recorded yet
    await limiter.fail("t_check", who, window_s=30)
    await limiter.check("t_check", who, limit=2)  # 1 < 2: still allowed
    await limiter.fail("t_check", who, window_s=30)
    with pytest.raises(RateLimitExceededError):
        await limiter.check("t_check", who, limit=2)
    assert int(await infra.redis.get(f"rl:t_check:{who}") or 0) == 2  # check never counted
    await limiter.reset("t_check", who)
    await limiter.check("t_check", who, limit=2)


async def test_buckets_and_subjects_are_independent(infra: Infrastructure) -> None:
    limiter, a, b = RateLimiter(infra.redis), subject(), subject()
    await limiter.hit("t_ind1", a, limit=1, window_s=30)
    await limiter.hit("t_ind1", b, limit=1, window_s=30)
    await limiter.hit("t_ind2", a, limit=1, window_s=30)
    with pytest.raises(RateLimitExceededError):
        await limiter.hit("t_ind1", a, limit=1, window_s=30)
