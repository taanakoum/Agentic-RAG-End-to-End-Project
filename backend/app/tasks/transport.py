"""Where the outbox dispatcher sends work: the Celery broker (default) or the in-process runner."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol

from app.core.config import Settings


class TaskSender(Protocol):
    def send_task(
        self,
        name: str,
        args: Sequence[Any] | None = ...,
        kwargs: dict[str, Any] | None = ...,
        **options: Any,
    ) -> Any: ...


def task_sender(settings: Settings) -> TaskSender:
    if settings.task_runner == "inline":
        from app.tasks.inline import get_inline_runner

        return get_inline_runner(settings)
    from app.tasks.celery_app import celery_app

    return celery_app
