"""PostgreSQL-backed LangGraph checkpointer (never in-memory), shared per process.

One `AsyncConnectionPool` per process feeds `AsyncPostgresSaver`. The pool is opened lazily on
first use (so processes that never run a graph hold no extra connections). Table creation
(`setup()`, idempotent, versioned by the library) is serialised across processes with a
PostgreSQL advisory lock so several API workers starting together cannot race.

psycopg's async mode cannot run on Windows' default ProactorEventLoop. The server therefore starts
with `--loop app.chat.checkpointer:selector_event_loop` (scripts/stack.py does), and the tests get
the same loop from pytest-asyncio's loop-factory hook; no event-loop POLICY is touched (policies are
deprecated from Python 3.14). Linux containers are unaffected.
"""

from __future__ import annotations

import asyncio
import contextlib

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app.core.config import Settings
from app.core.logging import get_logger

log = get_logger(__name__)

_SETUP_LOCK_KEY = 7_262_024_001  # arbitrary, project-wide constant for pg_advisory_lock


def selector_event_loop() -> asyncio.AbstractEventLoop:
    """An event loop psycopg's async mode can use on every platform.

    uvicorn (0.54) creates a ProactorEventLoop on Windows whatever the event-loop POLICY says, and
    psycopg refuses to run async on it, so the LangGraph checkpointer (and therefore every chat
    turn) fails on a real Windows server although the tests (which set the policy) pass. Start the
    server with:  uvicorn app.main:app --loop app.chat.checkpointer:selector_event_loop
    (scripts/stack.py does this on Windows; Linux containers are unaffected).
    """
    return asyncio.SelectorEventLoop()


def conninfo(settings: Settings) -> str:
    url = settings.database_url(driver="psycopg").set(drivername="postgresql")
    return url.render_as_string(hide_password=False)


class CheckpointerProvider:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._pool: AsyncConnectionPool | None = None  # pyright: ignore[reportMissingTypeArgument]
        self._saver: AsyncPostgresSaver | None = None
        self._lock = asyncio.Lock()
        self._step = "idle"  # what the first initialisation is doing (for the timeout log)

    @property
    def ready(self) -> bool:
        return self._saver is not None

    async def warm_up(self) -> bool:
        """Initialise now (called from a background task at start-up). Never raises.

        The first chat turn then does not pay for opening the pool and for table setup; if this
        fails (e.g. another backend holds the setup advisory lock) the lazy path in `get` simply
        tries again on the first request.
        """
        try:
            await self.get()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # a failed warm-up must never matter to start-up
            log.warning("checkpointer_warmup_failed", error_type=type(exc).__name__)
            return False
        log.info("checkpointer_warmup_done")
        return True

    async def get(self) -> AsyncPostgresSaver:
        if self._saver is not None:
            return self._saver
        async with self._lock:
            if self._saver is None:
                try:
                    await asyncio.wait_for(
                        self._initialise(), timeout=self._settings.checkpoint_init_timeout_s
                    )
                except TimeoutError:
                    # most likely another backend holds the setup advisory lock (e.g. a killed
                    # process whose connection PostgreSQL has not yet noticed): fail the turn
                    # cleanly now; the next request tries again from scratch
                    log.error("checkpointer_init_timeout", step=self._step)
                    await self._abandon()
                    raise
        assert self._saver is not None  # noqa: S101 - narrowing for the type checker
        return self._saver

    async def _initialise(self) -> None:
        self._step = "open_pool"
        pool: AsyncConnectionPool = AsyncConnectionPool(  # pyright: ignore[reportMissingTypeArgument]
            conninfo(self._settings),
            min_size=1,
            max_size=self._settings.checkpoint_pool_size,
            kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
            open=False,
        )
        self._pool = pool
        await pool.open(wait=True, timeout=self._settings.db_pool_timeout_s)
        saver = AsyncPostgresSaver(pool)  # pyright: ignore[reportArgumentType]
        async with pool.connection() as guard:
            self._step = "wait_for_setup_lock"
            await guard.execute("SELECT pg_advisory_lock(%s)", (_SETUP_LOCK_KEY,))
            try:
                self._step = "create_tables"
                await saver.setup()
            finally:
                await guard.execute("SELECT pg_advisory_unlock(%s)", (_SETUP_LOCK_KEY,))
        self._saver = saver
        self._step = "ready"
        log.info("checkpointer_ready")

    async def _abandon(self) -> None:
        pool, self._pool, self._saver = self._pool, None, None
        if pool is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(pool.close(), timeout=5)

    async def close(self) -> None:
        pool, self._pool, self._saver = self._pool, None, None
        if pool is not None:
            await pool.close()
