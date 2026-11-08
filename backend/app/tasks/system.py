"""System tasks (health probing). Domain tasks arrive in later phases."""

from __future__ import annotations

from app.tasks.celery_app import celery_app


@celery_app.task(name="system.echo")
def echo(value: str) -> str:
    """Round-trips a value through broker -> worker -> result backend."""
    return value
