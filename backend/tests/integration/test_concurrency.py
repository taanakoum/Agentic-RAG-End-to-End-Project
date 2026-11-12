"""Many users at once: the event loop must stay responsive and users must stay isolated."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx2
import pytest

from app.core.config import get_settings
from tests.integration.docs_helpers import API, PASSWORD, bearer, upload
from tests.pdfs import sample_text_pdf

pytestmark = pytest.mark.integration

USERS = 12


class LoopLag:
    """Measures how late the event loop wakes a 10 ms timer: any blocking call shows up here."""

    def __init__(self) -> None:
        self.max_lag = 0.0
        self.samples: list[float] = []
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def _run(self) -> None:
        while not self._stop.is_set():
            before = time.perf_counter()
            await asyncio.sleep(0.01)
            lag = time.perf_counter() - before - 0.01
            self.samples.append(lag)
            self.max_lag = max(self.max_lag, lag)

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await self._task


async def register_all(client: httpx2.AsyncClient) -> list[str]:
    emails = [f"user{i}@example.com" for i in range(USERS)]
    results = await asyncio.gather(
        *(
            client.post(f"{API}/auth/register", json={"email": e, "password": PASSWORD})
            for e in emails
        )
    )
    assert all(r.status_code == 201 for r in results)
    return emails


async def test_event_loop_stays_responsive_under_concurrent_logins_uploads_and_reads(
    client_with_settings: Any,
) -> None:
    # PRODUCTION-strength Argon2 (64 MiB, t=3, p=4, ~100+ ms per hash). With the cheap parameters
    # used elsewhere in the suite a hash takes ~5 ms and a blocking call would go unnoticed.
    strong = get_settings().model_copy(
        update={"argon2_memory_kib": 65536, "argon2_time_cost": 3, "argon2_parallelism": 4}
    )
    client: httpx2.AsyncClient = await client_with_settings(strong)
    emails = await register_all(client)
    logins = await asyncio.gather(
        *(client.post(f"{API}/auth/login", json={"email": e, "password": PASSWORD}) for e in emails)
    )
    headers = [bearer(r.json()["access_token"]) for r in logins]

    lag = LoopLag()
    lag.start()
    started = time.perf_counter()
    # a burst mixing every kind of work: Argon2 verification (threads), sandboxed PDF
    # inspection, file storage, hashing, database reads
    jobs = [
        client.post(f"{API}/auth/login", json={"email": e, "password": PASSWORD}) for e in emails
    ]
    jobs += [upload(client, h, sample_text_pdf(f"burst-{i}")) for i, h in enumerate(headers)]
    jobs += [client.get(f"{API}/documents", headers=h) for h in headers for _ in range(3)]
    responses = await asyncio.gather(*jobs)
    elapsed = time.perf_counter() - started
    await lag.stop()

    assert all(r.status_code in {200, 202} for r in responses), [
        r.status_code for r in responses if r.status_code not in {200, 202}
    ]
    # No blocking call on the loop. One slow sample can be a GC pause or the OS scheduler on a busy
    # machine, but a blocking call shows up as MANY long stalls (one per blocked hash): count them.
    # A blocked hash is ~100+ ms EACH: blocking Argon2 on the loop gave 3-4 stalls (up to 592 ms)
    # in mutation checks; a busy full-suite run produces at most one or two scheduler/GC stalls.
    long_stalls = sorted((lag_s for lag_s in lag.samples if lag_s > 0.15), reverse=True)
    assert len(long_stalls) <= 2, (
        f"{len(long_stalls)} event-loop stalls over 150 ms: "
        f"{[round(x * 1000) for x in long_stalls[:8]]} ms"
    )
    assert lag.max_lag < 0.8, f"event loop was blocked for {lag.max_lag * 1000:.0f} ms"
    assert elapsed < 30


async def test_concurrent_users_only_ever_see_their_own_documents(
    client: httpx2.AsyncClient,
) -> None:
    emails = await register_all(client)
    logins = await asyncio.gather(
        *(client.post(f"{API}/auth/login", json={"email": e, "password": PASSWORD}) for e in emails)
    )
    headers = [bearer(r.json()["access_token"]) for r in logins]
    uploads = await asyncio.gather(
        *(upload(client, h, sample_text_pdf(f"mine-{i}")) for i, h in enumerate(headers))
    )
    own_ids = [u.json()["document"]["id"] for u in uploads]
    assert len(set(own_ids)) == USERS

    listings = await asyncio.gather(
        *(client.get(f"{API}/documents", headers=h) for h in headers for _ in range(4))
    )
    for index, listing in enumerate(listings):
        user = index // 4
        assert [d["id"] for d in listing.json()["items"]] == [own_ids[user]]

    # and cannot open each other's documents, even all at the same time
    cross = await asyncio.gather(
        *(
            client.get(f"{API}/documents/{own_ids[(i + 1) % USERS]}", headers=headers[i])
            for i in range(USERS)
        )
    )
    assert all(r.status_code == 404 for r in cross)
