"""Run the background tasks inside the API process (free hosting without a Celery worker; ADR-042).

The durable parts are unchanged: work is described by PostgreSQL outbox rows, every task is
idempotent, and the reconciler recovers anything a crash or a sleeping host interrupted. Only the
transport changes: instead of publishing to a Redis broker for a separate worker, the dispatcher
hands events to this runner, which executes the same task functions in one background thread.

* One runner per deployment: the scheduler holds a PostgreSQL advisory lock; a second process
  (or a second API worker) stays on standby and only executes tasks it was handed directly.
* Retries: calling a task function directly makes `self.retry(...)` re-raise the original error
  (Celery's documented direct-call behaviour), so the runner reschedules with exponential backoff
  and gives up after a bounded number of attempts; the persisted state machine and the
  reconciler take over from there.
* Start-up: the reconciler runs first, so work cut off when the host went to sleep resumes.
"""

from __future__ import annotations

import heapq
import itertools
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from app.core.config import Settings
from app.core.logging import get_logger

log = get_logger(__name__)

# Distinct from the checkpointer's setup lock; any constant works as long as it is unique here.
SCHEDULER_LOCK_KEY = 0x52414731  # "RAG1"
_MAX_BACKOFF_S = 300.0


@dataclass(order=True)
class _Job:
    due: float
    seq: int
    name: str = field(compare=False)
    args: list[Any] = field(compare=False)
    attempt: int = field(compare=False, default=0)


class InlineRunner:
    """A minimal in-process task queue with a periodic scheduler."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._heap: list[_Job] = []
        self._seq = itertools.count()
        self._cv = threading.Condition()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self.leader = False
        self.executed = 0
        self.failed = 0
        self.max_attempts = settings.ingestion_max_attempts + 5

    # ------------------------------------------------------------------ the dispatcher's API

    def send_task(
        self,
        name: str,
        args: Sequence[Any] | None = None,
        kwargs: dict[str, Any] | None = None,
        **options: Any,
    ) -> None:
        """Same call shape as `Celery.send_task`, so `dispatch_outbox` works unchanged."""
        del kwargs, options  # the outbox sends positional args only; tracing headers are dropped
        self._push(name, list(args or []), attempt=0, delay=0.0)

    def _push(self, name: str, args: list[Any], *, attempt: int, delay: float) -> None:
        with self._cv:
            heapq.heappush(
                self._heap, _Job(time.monotonic() + delay, next(self._seq), name, args, attempt)
            )
            self._cv.notify()

    # ------------------------------------------------------------------ lifecycle

    @property
    def running(self) -> bool:
        return bool(self._threads) and all(t.is_alive() for t in self._threads)

    def start(self) -> None:
        if self._threads:
            return
        self._stop.clear()
        for target, name in (
            (self._work_loop, "inline-worker"),
            (self._schedule_loop, "inline-beat"),
        ):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)
        log.info("inline_runner_started")

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        with self._cv:
            self._cv.notify_all()
        for thread in self._threads:
            thread.join(timeout)
        self._threads.clear()
        log.info("inline_runner_stopped", executed=self.executed, failed=self.failed)

    # ------------------------------------------------------------------ execution

    def _next_job(self) -> _Job | None:
        with self._cv:
            while not self._stop.is_set():
                if self._heap:
                    wait = self._heap[0].due - time.monotonic()
                    if wait <= 0:
                        return heapq.heappop(self._heap)
                    self._cv.wait(min(wait, 1.0))
                else:
                    self._cv.wait(1.0)
            return None

    def run_job(self, job: _Job) -> bool:
        """Execute one job; reschedule it on failure. Returns True on success."""
        from app.tasks.celery_app import celery_app

        task = celery_app.tasks.get(job.name)
        if task is None:
            log.error("inline_unknown_task", task=job.name)
            return False
        try:
            task(*job.args)  # direct call: retry() re-raises instead of contacting a broker
        except Exception as exc:
            attempt = job.attempt + 1
            if attempt >= self.max_attempts:
                self.failed += 1
                log.error(
                    "inline_task_gave_up", task=job.name, attempts=attempt, error=type(exc).__name__
                )
                return False
            delay = min(5.0 * 2 ** (attempt - 1), _MAX_BACKOFF_S)
            log.warning(
                "inline_task_retry",
                task=job.name,
                attempt=attempt,
                delay_s=delay,
                error=type(exc).__name__,
            )
            self._push(job.name, job.args, attempt=attempt, delay=delay)
            return False
        self.executed += 1
        return True

    def _work_loop(self) -> None:
        while (job := self._next_job()) is not None:
            self.run_job(job)

    # ------------------------------------------------------------------ periodic work

    def _periodic(self) -> list[tuple[str, float]]:
        s = self._settings
        return [
            ("ingestion.reconcile", float(s.reconcile_interval_s)),
            ("ingestion.sweep_orphans", float(s.orphan_sweep_interval_s)),
            ("memory.sweep_retention", float(s.memory_retention_sweep_interval_s)),
            ("chat.sweep_checkpoints", float(s.orphan_sweep_interval_s)),
        ]

    def _dispatch_outbox(self) -> None:
        from app.ingestion.outbox import dispatch_outbox
        from app.ingestion.runtime import get_runtime

        rt = get_runtime()
        dispatch_outbox(rt.session_factory, self, batch_size=self._settings.outbox_batch_size)

    def _schedule_loop(self) -> None:
        import psycopg

        from app.chat.checkpointer import conninfo

        lock_conn: psycopg.Connection[Any] | None = None
        try:
            while not self._stop.is_set() and lock_conn is None:
                try:
                    conn = psycopg.connect(conninfo(self._settings), autocommit=True)
                    row = conn.execute(
                        "SELECT pg_try_advisory_lock(%s)", (SCHEDULER_LOCK_KEY,)
                    ).fetchone()
                    if row and row[0]:
                        lock_conn = conn
                        break
                    conn.close()
                    log.info("inline_scheduler_standby")
                except Exception as exc:
                    log.warning("inline_scheduler_lock_failed", error=type(exc).__name__)
                self._stop.wait(30.0)
            if lock_conn is None:
                return
            self.leader = True
            log.info("inline_scheduler_leader")
            # Resume whatever the last process left unfinished (the host may have slept mid-task).
            self._push("ingestion.reconcile", [], attempt=0, delay=0.0)
            due = {name: time.monotonic() + interval for name, interval in self._periodic()}
            next_dispatch = 0.0
            while not self._stop.is_set():
                now = time.monotonic()
                if now >= next_dispatch:
                    try:
                        self._dispatch_outbox()
                    except Exception as exc:
                        log.warning("inline_dispatch_failed", error=type(exc).__name__)
                    next_dispatch = now + float(self._settings.outbox_dispatch_interval_s)
                for name, interval in self._periodic():
                    if now >= due[name]:
                        self._push(name, [], attempt=0, delay=0.0)
                        due[name] = now + interval
                self._stop.wait(1.0)
        finally:
            self.leader = False
            if lock_conn is not None:
                lock_conn.close()  # releases the advisory lock


_runner: InlineRunner | None = None
_runner_lock = threading.Lock()


def get_inline_runner(settings: Settings) -> InlineRunner:
    global _runner
    with _runner_lock:
        if _runner is None:
            _runner = InlineRunner(settings)
        return _runner


def reset_inline_runner() -> None:
    """Tests: forget the singleton (after stopping it)."""
    global _runner
    with _runner_lock:
        _runner = None
