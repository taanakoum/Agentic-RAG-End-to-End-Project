"""The event-loop factory the real server must use (Windows: uvicorn ignores the loop policy).

Found by running the actual stack: with uvicorn's own ProactorEventLoop every chat turn failed with
"Psycopg cannot use the 'ProactorEventLoop'" although the whole test suite passed, because the tests
install the selector-loop policy themselves.
"""

from __future__ import annotations

import asyncio

import psycopg
import pytest
import uvicorn

from app.chat.checkpointer import conninfo, selector_event_loop
from app.core.config import get_settings

pytestmark = pytest.mark.integration


def test_the_factory_makes_a_selector_loop() -> None:
    loop = selector_event_loop()
    try:
        assert isinstance(loop, asyncio.SelectorEventLoop)
    finally:
        loop.close()


def test_uvicorn_can_load_the_factory_by_its_import_string() -> None:
    config = uvicorn.Config("app.main:app", loop="app.chat.checkpointer:selector_event_loop")
    factory = config.get_loop_factory()
    assert factory is selector_event_loop


def test_psycopg_async_works_on_the_factorys_loop_against_the_real_database() -> None:
    async def ask() -> int:
        async with await psycopg.AsyncConnection.connect(conninfo(get_settings())) as connection:
            row = await (await connection.execute("SELECT 41 + 1")).fetchone()
        assert row is not None
        return int(row[0])

    with asyncio.Runner(loop_factory=selector_event_loop) as runner:
        assert runner.run(ask()) == 42


@pytest.mark.skipif(not hasattr(asyncio, "ProactorEventLoop"), reason="Windows only")
def test_psycopg_async_refuses_the_proactor_loop_which_is_why_the_factory_exists() -> None:
    async def ask() -> None:
        async with await psycopg.AsyncConnection.connect(conninfo(get_settings())):
            pass

    with (
        pytest.raises(psycopg.errors.InterfaceError, match="ProactorEventLoop"),
        asyncio.Runner(loop_factory=asyncio.ProactorEventLoop) as runner,  # pyright: ignore[reportAttributeAccessIssue]
    ):
        runner.run(ask())
