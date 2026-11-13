from __future__ import annotations

import json
import logging
from collections.abc import Iterator

import pytest

from app.core.logging import configure_logging, get_logger, redact_processor
from app.core.request_context import sanitize_request_id, set_request_id


def test_redaction_of_sensitive_keys() -> None:
    out = redact_processor(
        None,
        "info",
        {
            "event": "login",
            "password": "p",
            "access_token": "t",
            "nested": {"Authorization": "Bearer x", "ok": 1},
            "items": [{"api_key": "k"}],
            "email": "a@b.c",
        },
    )
    assert out["password"] == "[REDACTED]"
    assert out["access_token"] == "[REDACTED]"
    assert out["nested"] == {"Authorization": "[REDACTED]", "ok": 1}
    assert out["items"] == [{"api_key": "[REDACTED]"}]
    assert out["email"] == "a@b.c"
    assert out["event"] == "login"


def test_json_log_contains_request_id_and_redacts(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(level="INFO", json_output=True, service="test-svc")
    set_request_id("req-12345678")
    try:
        get_logger("t").info("hello", password="secret-value", n=1)
    finally:
        set_request_id(None)
    line = capsys.readouterr().out.strip().splitlines()[-1]
    data = json.loads(line)
    assert data["event"] == "hello"
    assert data["request_id"] == "req-12345678"
    assert data["service"] == "test-svc"
    assert data["password"] == "[REDACTED]"
    assert "secret-value" not in line


@pytest.mark.parametrize(
    "bad",
    [None, "", "short", "has space in it!", "x" * 65, "a/b/c/d/e/f/g", "line\nbreak12"],
)
def test_invalid_request_ids_are_replaced(bad: str | None) -> None:
    result = sanitize_request_id(bad)
    assert result != bad
    assert len(result) == 32


def test_valid_request_id_is_kept() -> None:
    assert sanitize_request_id("abc-DEF_123.456") == "abc-DEF_123.456"


def test_console_tracebacks_never_print_local_variables(capsys: pytest.CaptureFixture[str]) -> None:
    """rich tracebacks print local variables (secrets!); the console renderer must not use them."""
    configure_logging(level="INFO", json_output=False, service="t")

    def handler() -> None:
        local_secret = "LOCAL-SECRET-VALUE-12345"  # a local variable on purpose
        raise RuntimeError(f"boom {len(local_secret)}")

    try:
        handler()
    except RuntimeError as exc:
        get_logger("t").error("failed", exc_info=exc)
    out = capsys.readouterr().out
    assert "RuntimeError" in out
    assert "LOCAL-SECRET-VALUE-12345" not in out


# ------------------------------------------------------------------ KI-008: known Celery noise


class _Collect(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def bootsteps() -> Iterator[tuple[logging.Logger, _Collect]]:
    from app.core.logging import KnownCeleryNoiseFilter, install_celery_noise_filter

    logger = logging.getLogger("celery.bootsteps")
    saved_filters, saved_level, saved_handlers = (
        list(logger.filters),
        logger.level,
        list(logger.handlers),
    )
    logger.filters[:] = [f for f in logger.filters if not isinstance(f, KnownCeleryNoiseFilter)]
    install_celery_noise_filter()
    install_celery_noise_filter()  # idempotent
    assert sum(isinstance(f, KnownCeleryNoiseFilter) for f in logger.filters) == 1
    collector = _Collect()
    logger.addHandler(collector)
    logger.setLevel(logging.INFO)
    yield logger, collector
    logger.removeHandler(collector)
    logger.filters[:] = saved_filters
    logger.handlers[:] = saved_handlers
    logger.setLevel(saved_level)


BENIGN = AttributeError("'NoneType' object has no attribute 'connection_errors'")


def test_the_known_benign_celery_message_is_not_an_error_any_more(
    bootsteps: tuple[logging.Logger, _Collect],
) -> None:
    logger, collector = bootsteps
    logger.error("Error on %s %s: %r", "restarting", "Gossip", BENIGN, exc_info=True)
    assert collector.records == []  # INFO level: the downgraded record is below the threshold
    logger.setLevel(logging.DEBUG)
    logger.error("Error on %s %s: %r", "restarting", "Gossip", BENIGN)
    assert [r.levelno for r in collector.records] == [logging.DEBUG]
    assert "known benign" in collector.records[0].getMessage()


def test_other_errors_of_the_same_logger_still_reach_the_log(
    bootsteps: tuple[logging.Logger, _Collect],
) -> None:
    logger, collector = bootsteps
    logger.error(
        "Error on %s %s: %r",
        "restarting",
        "Gossip",
        AttributeError("'NoneType' object has no attribute 'foo'"),
    )
    logger.error("Error on %s %s: %r", "starting", "Consumer", RuntimeError("boom"))
    logger.warning("'NoneType' object has no attribute 'connection_errors'")  # not an ERROR
    logger.error("something else: 'NoneType' object has no attribute 'connection_errors'")
    assert [r.levelno for r in collector.records] == [
        logging.ERROR,
        logging.ERROR,
        logging.WARNING,
        logging.ERROR,
    ]


def test_other_loggers_are_not_filtered(bootsteps: tuple[logging.Logger, _Collect]) -> None:
    other = logging.getLogger("celery.worker.consumer")
    collector = _Collect()
    other.addHandler(collector)
    try:
        other.error("Error on %s %s: %r", "restarting", "Gossip", BENIGN)
    finally:
        other.removeHandler(collector)
    assert [r.levelno for r in collector.records] == [logging.ERROR]
