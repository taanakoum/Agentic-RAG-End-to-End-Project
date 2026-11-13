"""Structured logging (structlog) with request IDs and secret redaction."""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import MutableMapping
from typing import Any

import structlog

from app.core.request_context import get_request_id

_REDACTED = "[REDACTED]"
_SENSITIVE_PARTS = ("password", "token", "secret", "authorization", "api_key", "apikey", "cookie")


def _is_sensitive(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in _SENSITIVE_PARTS)


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: (_REDACTED if isinstance(k, str) and _is_sensitive(k) else _redact(v))
            for k, v in value.items()  # pyright: ignore[reportUnknownVariableType]
        }
    if isinstance(value, (list, tuple)):
        return [_redact(v) for v in value]  # pyright: ignore[reportUnknownVariableType]
    return value


def redact_processor(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    for key in list(event_dict):
        if key != "event" and _is_sensitive(key):
            event_dict[key] = _REDACTED
        else:
            event_dict[key] = _redact(event_dict[key])
    return event_dict


def add_request_id(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    request_id = get_request_id()
    if request_id is not None:
        event_dict.setdefault("request_id", request_id)
    return event_dict


def add_trace_ids(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Put the active trace/span id on every log line, so logs and traces can be joined."""
    from app.core.telemetry import trace_ids

    ids = trace_ids()
    if ids is not None:
        event_dict.setdefault("trace_id", ids[0])
        event_dict.setdefault("span_id", ids[1])
    return event_dict


_BENIGN_CELERY_TEARDOWN = re.compile(
    r"^Error on .*'NoneType' object has no attribute 'connection_errors'"
)


class KnownCeleryNoiseFilter(logging.Filter):
    """Downgrade ONE known-benign upstream message from ERROR to DEBUG (KI-008).

    When Redis restarts under a running worker, kombu closes a connection that is already gone and
    Celery logs `Error on restarting Gossip: AttributeError("'NoneType' object has no attribute
    'connection_errors'")` with a long traceback, although the worker reconnects and keeps working.
    Celery/kombu 5.7.0 (the newest releases) still do this. Only an ERROR record of the
    `celery.bootsteps` logger whose message matches this exact text is affected; everything else
    passes untouched, and the downgraded record is still available at DEBUG.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno != logging.ERROR or not _BENIGN_CELERY_TEARDOWN.match(
            record.getMessage()
        ):
            return True
        target = logging.getLogger(record.name)
        if target.isEnabledFor(logging.DEBUG):
            target.debug("%s (known benign, downgraded)", record.getMessage())
        return False


def install_celery_noise_filter() -> None:
    logger = logging.getLogger("celery.bootsteps")
    if not any(isinstance(f, KnownCeleryNoiseFilter) for f in logger.filters):
        logger.addFilter(KnownCeleryNoiseFilter())


def configure_logging(*, level: str, json_output: bool, service: str) -> None:
    """Configure structlog and route stdlib logging (uvicorn, celery, sqlalchemy) through it."""
    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        add_request_id,
        add_trace_ids,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        redact_processor,
    ]
    renderer: Any = (
        structlog.processors.JSONRenderer()
        if json_output
        else structlog.dev.ConsoleRenderer(
            colors=False,
            # Never rich tracebacks: they print local variable values (secrets!) and are very slow.
            exception_formatter=structlog.dev.plain_traceback,
        )
    )
    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info if json_output else _noop,
            renderer,
        ],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    for noisy in ("uvicorn.access",):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    install_celery_noise_filter()
    structlog.contextvars.bind_contextvars(service=service)


def _noop(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    return event_dict


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.stdlib.get_logger(name)
