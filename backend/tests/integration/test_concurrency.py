"""Many users at once: the event loop must stay responsive and users must stay isolated."""

from __future__ import annotations

import asyncio
import functools
import threading
import time
from collections.abc import Callable
from typing import Any

import httpx2
import pytest

from app.core.config import get_settings
from app.core.security import PasswordService
from app.ingestion.parsing import SandboxedPdfParser
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


def record_threads(
    monkeypatch: pytest.MonkeyPatch, owner: type[Any], name: str, calls: list[int]
) -> None:
    """Wrap `owner.name` so that the thread id of every call is appended to `calls`."""
    original: Callable[..., Any] = getattr(owner, name)

    @functools.wraps(original)
    def recorder(*args: Any, **kwargs: Any) -> Any:
        calls.append(threading.get_ident())
        return original(*args, **kwargs)

    monkeypatch.setattr(owner, name, recorder)


async def test_event_loop_stays_responsive_under_concurrent_logins_uploads_and_reads(
    client_with_settings: Any, monkeypatch: pytest.MonkeyPatch
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

    # PRIMARY, deterministic check: record the thread of every Argon2 operation and of every PDF
    # inspection during the burst. A blocking call on the event loop would show the loop's thread.
    loop_thread = threading.get_ident()
    hash_threads: list[int] = []
    inspect_threads: list[int] = []
    record_threads(monkeypatch, PasswordService, "verify_sync", hash_threads)
    record_threads(monkeypatch, PasswordService, "hash_sync", hash_threads)
    record_threads(monkeypatch, SandboxedPdfParser, "inspect", inspect_threads)

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
    assert len(hash_threads) >= USERS, "the burst should have verified every login"
    assert loop_thread not in hash_threads, "Argon2 ran on the event-loop thread"
    assert len(inspect_threads) >= USERS, "the burst should have inspected every upload"
    assert loop_thread not in inspect_threads, "PDF inspection ran on the event-loop thread"

    # SECONDARY, loose wall-clock signal: it can only catch a stall of some other origin (the
    # thread checks above are the real guard, because wall-clock limits flaked on a loaded laptop,
    # fixed in item 3 of the remaining-issues plan), so the limit is far above scheduler/GC noise.
    assert lag.max_lag < 2.0, f"event loop was blocked for {lag.max_lag * 1000:.0f} ms"
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
