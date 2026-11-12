"""The first use of the checkpointer must never hang a chat turn forever.

Seen once while running the real stack: the first streamed turn after a start produced only
keep-alives. The initialisation takes a PostgreSQL advisory lock; anything else holding that lock
(a leftover backend of a killed process) blocks it indefinitely. It is now time-bounded, logged by
step, and the next attempt starts from scratch.
"""

from __future__ import annotations

import asyncio

import psycopg
import pytest

from app.chat.checkpointer import _SETUP_LOCK_KEY, CheckpointerProvider, conninfo  # pyright: ignore[reportPrivateUsage]
from app.core.config import get_settings

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
