"""The first use of the checkpointer must never hang a chat turn forever.

Seen once while running the real stack: the first streamed turn after a start produced only
keep-alives. The initialisation takes a PostgreSQL advisory lock; anything else holding that lock
(a leftover backend of a killed process) blocks it indefinitely. It is now time-bounded, logged by
step, and the next attempt starts from scratch.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import psycopg
import pytest

from app.chat.checkpointer import _SETUP_LOCK_KEY, CheckpointerProvider, conninfo  # pyright: ignore[reportPrivateUsage]
from app.core.config import get_settings
from app.infra.container import Infrastructure
from app.main import create_app

pytestmark = pytest.mark.integration


async def test_a_held_setup_lock_times_out_cleanly_and_the_next_attempt_works() -> None:
    settings = get_settings().model_copy(update={"checkpoint_init_timeout_s": 1.5})
    blocker = await psycopg.AsyncConnection.connect(conninfo(settings), autocommit=True)
    provider = CheckpointerProvider(settings)
    try:
        await blocker.execute("SELECT pg_advisory_lock(%s)", (_SETUP_LOCK_KEY,))
        started = asyncio.get_running_loop().time()
        with pytest.raises(TimeoutError):
            await provider.get()
        assert asyncio.get_running_loop().time() - started < 10  # bounded, not forever
        assert provider._step == "wait_for_setup_lock"  # pyright: ignore[reportPrivateUsage]
        assert provider._saver is None and provider._pool is None  # pyright: ignore[reportPrivateUsage]

        await blocker.execute("SELECT pg_advisory_unlock(%s)", (_SETUP_LOCK_KEY,))
        saver = await provider.get()  # the provider recovered: a full fresh initialisation
        assert saver is not None
        assert await provider.get() is saver  # and it is cached afterwards
    finally:
        await blocker.close()
        await provider.close()


async def test_concurrent_first_uses_share_one_initialisation() -> None:
    provider = CheckpointerProvider(get_settings())
    try:
        savers = await asyncio.gather(*(provider.get() for _ in range(5)))
        assert len({id(s) for s in savers}) == 1
    finally:
        await provider.close()


async def _wait_until(condition: Callable[[], bool], seconds: float = 20.0) -> bool:
    deadline = asyncio.get_running_loop().time() + seconds
    while asyncio.get_running_loop().time() < deadline:
        if condition():
            return True
        await asyncio.sleep(0.05)
    return condition()


async def test_start_up_warms_the_checkpointer_before_the_first_request() -> None:
    app = create_app(get_settings())
    async with app.router.lifespan_context(app):
        infra: Infrastructure = app.state.infra
        # nobody asked for the checkpointer: only the background warm-up can have initialised it
        assert await _wait_until(lambda: infra.checkpointer.ready), "not warmed at start-up"


async def test_a_failed_warm_up_does_not_block_start_up_and_the_first_request_still_works() -> None:
    settings = get_settings().model_copy(update={"checkpoint_init_timeout_s": 1.0})
    blocker = await psycopg.AsyncConnection.connect(conninfo(settings), autocommit=True)
    try:
        await blocker.execute("SELECT pg_advisory_lock(%s)", (_SETUP_LOCK_KEY,))
        app = create_app(settings)
        started = asyncio.get_running_loop().time()
        async with app.router.lifespan_context(app):
            assert asyncio.get_running_loop().time() - started < 5  # start-up did not wait
            infra: Infrastructure = app.state.infra
            await asyncio.sleep(2.5)  # the warm-up has timed out by now ...
            assert not infra.checkpointer.ready  # ... and left nothing half-initialised

            await blocker.execute("SELECT pg_advisory_unlock(%s)", (_SETUP_LOCK_KEY,))
            saver = await infra.checkpointer.get()  # the lazy path serves the first request
            assert saver is not None and infra.checkpointer.ready
    finally:
        await blocker.close()
