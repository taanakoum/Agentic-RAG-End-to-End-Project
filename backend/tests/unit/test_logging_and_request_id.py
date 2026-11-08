from __future__ import annotations

import json

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
