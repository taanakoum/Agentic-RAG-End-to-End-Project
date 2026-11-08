"""Celery application.

Execution model (ADR-003): tasks are plain synchronous functions that use synchronous clients
(SQLAlchemy sync engine, redis-py, qdrant-client). No asyncio.run() inside tasks.
"""

from __future__ import annotations

from celery import Celery
from celery.signals import setup_logging

from app.core.config import Settings, get_settings
from app.core.logging import configure_logging


def create_celery_app(settings: Settings | None = None) -> Celery:
    settings = settings or get_settings()
    app = Celery(
        "agentic_rag",
        broker=settings.redis_url,
        backend=settings.redis_url,
        include=["app.tasks.system", "app.tasks.ingestion"],
    )
    app.conf.update(
        task_serializer="json",
        accept_content=["json"],
        result_serializer="json",
        timezone="UTC",
        enable_utc=True,
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        worker_prefetch_multiplier=1,
        task_time_limit=settings.celery_task_time_limit_s,
        task_soft_time_limit=settings.celery_task_soft_time_limit_s,
        worker_concurrency=settings.celery_worker_concurrency,
        result_expires=3600,
        broker_connection_retry_on_startup=True,
        # Redis re-delivers unacked tasks after this; must exceed the hard time limit.
        broker_transport_options={"visibility_timeout": settings.celery_task_time_limit_s + 300},
        worker_hijack_root_logger=False,
        beat_schedule={
            "dispatch-outbox": {
                "task": "ingestion.dispatch_outbox",
                "schedule": settings.outbox_dispatch_interval_s,
            },
            "reconcile-ingestion": {
                "task": "ingestion.reconcile",
                "schedule": settings.reconcile_interval_s,
            },
            "sweep-orphans": {
                "task": "ingestion.sweep_orphans",
                "schedule": settings.orphan_sweep_interval_s,
            },
        },
    )
    return app


celery_app = create_celery_app()


def _configure_worker_logging(**_: object) -> None:
    settings = get_settings()
    configure_logging(
        level=settings.log_level, json_output=settings.log_json, service="agentic-rag-worker"
    )


setup_logging.connect(_configure_worker_logging)
