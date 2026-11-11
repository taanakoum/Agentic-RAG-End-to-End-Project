"""Free-tier quota guard: stop BEFORE the provider says no.

Free hosted models have hard daily and per-minute caps per model. Hitting them wastes attempts and
turns every user request into a 429. The guard counts every request that is about to reach the
provider (Redis counters shared by all processes) and refuses it locally when the configured
budget is used up, so the fallback chain moves to the next model or the user gets one clean
`MODEL_RATE_LIMITED` with an accurate `Retry-After` (seconds until the provider's day resets).

  * `LLM_DAILY_REQUEST_BUDGET`  requests per model per provider day (0 = unlimited)
  * `LLM_REQUESTS_PER_MINUTE`   requests per model per minute (0 = unlimited)
  * `LLM_QUOTA_TIMEZONE`        where the provider's day rolls over (Gemini: America/Los_Angeles)

Counters live in Redis and fail OPEN (a Redis problem never blocks chat). They count what THIS
application sends; the provider may also count other clients of the same key, so keep the budget
below the real cap (docs/DEPLOYMENT.md explains how it was calibrated).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from redis.asyncio import Redis as AsyncRedis
from redis.exceptions import RedisError

from app.core.config import Settings
from app.core.logging import get_logger

log = get_logger(__name__)

PREFIX = "llmq"
_REDIS_TIMEOUT_S = 0.25


@dataclass(frozen=True, slots=True)
class Decision:
    allowed: bool
    reason: str = ""  # "daily_budget" | "per_minute"
    retry_after_s: int = 0


@dataclass(frozen=True, slots=True)
class ModelUsage:
    model: str
    used_today: int
    daily_budget: int  # 0 = unlimited

    @property
    def remaining(self) -> int | None:
        return None if self.daily_budget == 0 else max(0, self.daily_budget - self.used_today)


def day_key(now: datetime) -> str:
    return now.strftime("%Y%m%d")


def seconds_until_next_day(now: datetime) -> int:
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(1, int((tomorrow - now).total_seconds()))


class QuotaGuard:
    def __init__(self, redis: AsyncRedis, settings: Settings) -> None:
        self._redis = redis
        self._settings = settings
        try:
            self._tz = ZoneInfo(settings.llm_quota_timezone)
        except Exception:  # unknown zone name: fall back to UTC rather than failing chat
            self._tz = ZoneInfo("UTC")

    def _now(self) -> datetime:
        return datetime.now(self._tz)

    @property
    def enabled(self) -> bool:
        s = self._settings
        return bool(s.llm_daily_request_budget or s.llm_requests_per_minute)

    async def reserve(self, model: str) -> Decision:
        """Count one request for `model`, or refuse it. Fails open."""
        s = self._settings
        if not self.enabled:
            return Decision(True)
        now = self._now()
        try:
            async with asyncio.timeout(_REDIS_TIMEOUT_S * 3):
                if s.llm_daily_request_budget:
                    key = f"{PREFIX}:day:{model}:{day_key(now)}"
                    used = int(await self._redis.incr(key))
                    if used == 1:
                        await self._redis.expire(key, 3 * 86400)
                    if used > s.llm_daily_request_budget:
                        await self._redis.decr(key)  # the refused request is not counted
                        return Decision(False, "daily_budget", seconds_until_next_day(now))
                if s.llm_requests_per_minute:
                    minute = int(time.time() // 60)
                    key = f"{PREFIX}:min:{model}:{minute}"
                    used = int(await self._redis.incr(key))
                    if used == 1:
                        await self._redis.expire(key, 120)
                    if used > s.llm_requests_per_minute:
                        await self._redis.decr(key)
                        if s.llm_daily_request_budget:  # give the day slot back as well
                            await self._redis.decr(f"{PREFIX}:day:{model}:{day_key(now)}")
                        return Decision(False, "per_minute", 60 - int(time.time() % 60))
        except (RedisError, OSError, TimeoutError) as exc:
            log.warning("llm_quota_unavailable", error=type(exc).__name__)
        return Decision(True)

    async def usage(self, models: list[str]) -> list[ModelUsage]:
        now = self._now()
        out: list[ModelUsage] = []
        for model in models:
            used = 0
            try:
                async with asyncio.timeout(_REDIS_TIMEOUT_S):
                    raw = await self._redis.get(f"{PREFIX}:day:{model}:{day_key(now)}")
                used = int(raw) if raw else 0
            except (RedisError, OSError, TimeoutError, ValueError):
                pass
            out.append(ModelUsage(model, used, self._settings.llm_daily_request_budget))
        return out

    def resets_in_s(self) -> int:
        return seconds_until_next_day(self._now())
