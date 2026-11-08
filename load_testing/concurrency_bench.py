"""Concurrency benchmark against a RUNNING API (real HTTP, real PostgreSQL/Redis/Qdrant).

    python load_testing/concurrency_bench.py --base http://127.0.0.1:8780 --users 40 --seconds 20

What it measures (all numbers are printed; nothing is estimated):
  * registration + login latency for N users arriving at once (Argon2id is CPU-heavy by design)
  * steady-state latency of authenticated reads while every user keeps issuing requests
  * upload latency (validation in the sandbox, storage, DB, outbox) under that load
  * event-loop health: a probe hits /health/live every 50 ms the whole time. If anything blocks the
    event loop, the probe latency spikes. This is the number that matters for "many users at once".
Report hardware and configuration alongside any number you quote from this script.
"""

from __future__ import annotations

import argparse
import asyncio
import random
import statistics
import sys
import time
import uuid
from pathlib import Path

import httpx2

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from tests.pdfs import sample_text_pdf  # noqa: E402

PASSWORD = "correct-horse-battery-staple"


def pct(values: list[float], p: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * p))]


def summary(name: str, values: list[float]) -> str:
    if not values:
        return f"{name:28} n=0"
    return (
        f"{name:28} n={len(values):5d}  p50={pct(values, 0.5) * 1000:7.0f} ms  "
        f"p95={pct(values, 0.95) * 1000:7.0f} ms  p99={pct(values, 0.99) * 1000:7.0f} ms  "
        f"max={max(values) * 1000:7.0f} ms  mean={statistics.mean(values) * 1000:7.0f} ms"
    )


async def timed(coro_factory, bucket: list[float], errors: list[str]):
    start = time.perf_counter()
    try:
        response = await coro_factory()
    except Exception as exc:  # network-level failure
        errors.append(type(exc).__name__)
        return None
    bucket.append(time.perf_counter() - start)
    if response.status_code >= 500 or response.status_code == 429:
        errors.append(f"HTTP {response.status_code}")
    return response


async def user_session(
    base: str, index: int, run_id: str, seconds: float, lat: dict[str, list[float]], errors: list[str], ready: asyncio.Event, go: asyncio.Event
) -> None:
    async with httpx2.AsyncClient(base_url=base, timeout=120) as client:
        email = f"bench-{run_id}-{index}@example.com"
        body = {"email": email, "password": PASSWORD}
        await timed(lambda: client.post("/api/v1/auth/register", json=body), lat["register"], errors)
        login = await timed(lambda: client.post("/api/v1/auth/login", json=body), lat["login"], errors)
        if login is None or login.status_code != 200:
            ready.set()
            return
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        ready.set()
        await go.wait()
        end = time.perf_counter() + seconds
        n = 0
        while time.perf_counter() < end:
            n += 1
            await timed(lambda: client.get("/api/v1/auth/me", headers=headers), lat["GET /auth/me"], errors)
            await timed(lambda: client.get("/api/v1/documents", headers=headers), lat["GET /documents"], errors)
            if n % 5 == 0:
                pdf = sample_text_pdf(f"{run_id}-{index}-{n}")
                files = {"file": ("bench.pdf", pdf, "application/pdf")}
                await timed(
                    lambda: client.post("/api/v1/documents/upload", files=files, headers=headers),
                    lat["POST /documents/upload"],
                    errors,
                )
            await asyncio.sleep(random.uniform(0.05, 0.25))  # think time


async def loop_probe(base: str, stop: asyncio.Event, out: list[float]) -> None:
    async with httpx2.AsyncClient(base_url=base, timeout=30) as client:
        while not stop.is_set():
            start = time.perf_counter()
            try:
                await client.get("/api/v1/health/live")
                out.append(time.perf_counter() - start)
            except Exception:
                out.append(30.0)
            await asyncio.sleep(0.05)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8780")
    parser.add_argument("--users", type=int, default=40)
    parser.add_argument("--seconds", type=float, default=20)
    args = parser.parse_args()
    run_id = uuid.uuid4().hex[:6]
    lat: dict[str, list[float]] = {k: [] for k in ("register", "login", "GET /auth/me", "GET /documents", "POST /documents/upload")}
    errors: list[str] = []
    probe: list[float] = []

    # baseline: the probe alone, before any load
    idle_stop = asyncio.Event()
    idle: list[float] = []
    task = asyncio.create_task(loop_probe(args.base, idle_stop, idle))
    await asyncio.sleep(2)
    idle_stop.set()
    await task

    stop = asyncio.Event()
    probe_task = asyncio.create_task(loop_probe(args.base, stop, probe))
    go = asyncio.Event()
    events = [asyncio.Event() for _ in range(args.users)]
    started = time.perf_counter()
    sessions = [
        asyncio.create_task(user_session(args.base, i, run_id, args.seconds, lat, errors, events[i], go))
        for i in range(args.users)
    ]
    await asyncio.gather(*(e.wait() for e in events))
    print(f"{args.users} users registered + logged in concurrently in {time.perf_counter() - started:.1f}s")
    go.set()
    await asyncio.gather(*sessions)
    stop.set()
    await probe_task

    print()
    print(summary("probe /health/live (idle)", idle))
    print(summary("probe /health/live (loaded)", probe))
    for name, values in lat.items():
        print(summary(name, values))
    total = sum(len(v) for v in lat.values())
    print(f"\nrequests: {total}, errors: {len(errors)} {sorted(set(errors))[:5]}")
    print(f"throughput during load phase: {sum(len(lat[k]) for k in lat if k.startswith(('GET', 'POST'))) / args.seconds:.1f} req/s")


if __name__ == "__main__":
    asyncio.run(main())
