"""Privacy rules and correlation helpers of the telemetry module (no services needed)."""

from __future__ import annotations

import json
import logging

import pytest
import structlog

from app.core.logging import add_trace_ids
from app.core.telemetry import (
    DENIED_KEYS,
    REDACTED,
    continue_trace,
    hashed_user,
    inject_traceparent,
    safe_attributes,
    set_attributes,
    span,
    trace_ids,
)
from tests import telemetry_helpers as th

# ------------------------------------------------------------------ attribute safety


@pytest.mark.parametrize(
    "key",
    [
        "prompt",
        "llm.prompt",
        "answer",
        "query",
        "user.query",
        "question",
        "message",
        "content",
        "text",
        "chunk_text",
        "excerpt",
        "quote",
        "filename",
        "file_name",
        "email",
        "user.email",
        "password",
        "api_key",
        "apikey",
        "authorization",
        "token",
        "access_token",
        "secret",
        "cookie",
        "memory",
        "history",
        "http.request.body",
    ],
)
def test_sensitive_attribute_keys_are_dropped_whatever_the_value(key: str) -> None:
    assert safe_attributes({key: "harmless looking value"}) == {}
    assert DENIED_KEYS.search(key)


def test_scalars_and_lists_of_scalars_are_kept_and_everything_else_is_dropped() -> None:
    clean = safe_attributes(
        {
            "count": 3,
            "ratio": 0.5,
            "flag": True,
            "model": "gemini-3.5-flash-lite",
            "degraded": ["rerank_timeout", "memory_unavailable"],
            "numbers": [1, 2.5, True],
            "nothing": None,
            "mapping": {"a": 1},
            "object": object(),
            "mixed": [1, {"x": 1}, "ok"],
        }
    )
    assert clean == {
        "count": 3,
        "ratio": 0.5,
        "flag": True,
        "model": "gemini-3.5-flash-lite",
        "degraded": ["rerank_timeout", "memory_unavailable"],
        "numbers": [1, 2.5, True],
        "mixed": [1, "ok"],
    }
    assert safe_attributes(None) == {} and safe_attributes({}) == {}


@pytest.mark.parametrize(
    "secret",
    [
        "dana.smith@example.com",
        "Bearer eyJhbGciOiJIUzI1NiJ9.payload.signature",
        "sk-ABCDEF0123456789abcdef01",
        "ghp_ABCDEF0123456789abcdef0123",
        "AQ.FAKEFAKEFAKEFAKE0000_synthetic-test-key-1234567890",
        "AIzaSyA1234567890abcdefghijklmnopqrstuv",
        "-----BEGIN RSA PRIVATE KEY-----",
    ],
)
def test_secret_looking_values_are_redacted_even_under_an_innocent_key(secret: str) -> None:
    clean = safe_attributes({"detail": f"failed for {secret} twice"})
    assert secret not in clean["detail"] and REDACTED in clean["detail"]
    assert safe_attributes({"items": [secret]})["items"] == [REDACTED]


def test_long_strings_are_truncated() -> None:
    assert len(safe_attributes({"note": "x" * 5000})["note"]) == 200


# ------------------------------------------------------------------ spans and correlation


def test_a_span_records_the_error_type_but_never_the_message() -> None:
    with pytest.raises(ValueError), span("demo.failing", attempt=2):
        raise ValueError("the user's secret document text: hunter2")
    (recorded,) = th.named("demo.failing")
    assert recorded.attributes is not None
    assert recorded.attributes["error.type"] == "ValueError" and recorded.attributes["attempt"] == 2
    assert "hunter2" not in th.serialized() and not recorded.events  # no exception event either


def test_span_attributes_are_filtered_at_every_entry_point() -> None:
    with span("demo.attrs", query="what is the secret", count=2, tag="a@b.co"):
        set_attributes(answer="private answer", extra=1)
    (recorded,) = th.named("demo.attrs")
    assert dict(recorded.attributes or {}) == {"count": 2, "tag": REDACTED, "extra": 1}


def test_trace_ids_and_traceparent_exist_only_inside_a_span() -> None:
    assert trace_ids() is None and inject_traceparent() is None
    with span("demo.ids"):
        ids = trace_ids()
        parent = inject_traceparent()
    assert ids is not None and len(ids[0]) == 32 and len(ids[1]) == 16
    assert parent is not None and parent.startswith(f"00-{ids[0]}-{ids[1]}-")


def test_a_trace_can_be_continued_from_a_stored_traceparent() -> None:
    with span("demo.request") as request:
        stored = inject_traceparent()
        request_trace = request.get_span_context().trace_id
    with continue_trace(stored), span("demo.background") as background:
        assert background.get_span_context().trace_id == request_trace
    (child,) = th.named("demo.background")
    (root,) = th.named("demo.request")
    assert child.parent is not None and root.context is not None
    assert child.parent.span_id == root.context.span_id  # a proper child, across processes
    with continue_trace(None), span("demo.orphan"):
        pass
    assert th.named("demo.orphan")[0].parent is None  # no header: a fresh root, no crash
    with continue_trace("garbage"), span("demo.garbage"):
        pass  # malformed header is ignored


def test_log_lines_carry_the_trace_ids_of_the_active_span() -> None:
    event: dict[str, object] = {"event": "something"}
    assert "trace_id" not in add_trace_ids(None, "info", dict(event))
    with span("demo.log"):
        ids = trace_ids()
        logged = add_trace_ids(None, "info", dict(event))
    assert ids is not None and logged["trace_id"] == ids[0] and logged["span_id"] == ids[1]


def test_structlog_output_includes_trace_ids_end_to_end(capsys: pytest.CaptureFixture[str]) -> None:
    from app.core.logging import configure_logging

    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    try:
        configure_logging(level="INFO", json_output=True, service="test")
        log = structlog.get_logger("trace-demo")
        with span("demo.structlog"):
            ids = trace_ids()
            log.info("inside_span")
        log.info("outside_span")
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
    assert ids is not None
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    inside = next(entry for entry in lines if entry["event"] == "inside_span")
    outside = next(entry for entry in lines if entry["event"] == "outside_span")
    assert inside["trace_id"] == ids[0] and inside["span_id"] == ids[1]
    assert "trace_id" not in outside


def test_user_ids_are_hashed_never_sent_raw() -> None:
    value = hashed_user("3f2b8c1e-0000-4000-8000-000000000001")
    assert "3f2b8c1e" not in value and len(value) == 32
    assert value == hashed_user("3f2b8c1e-0000-4000-8000-000000000001")  # stable pseudonym
    assert value != hashed_user("3f2b8c1e-0000-4000-8000-000000000002")


@pytest.mark.parametrize(
    ("statement", "operation"),
    [
        ("SELECT 1", "SELECT"),
        ("  select * from users", "SELECT"),
        ("(SELECT 1) UNION (SELECT 2)", "SELECT"),
        ("INSERT INTO t VALUES (1)", "INSERT"),
        ("update t set a = 1", "UPDATE"),
        ("DELETE FROM t", "DELETE"),
        ("WITH x AS (SELECT 1) SELECT * FROM x", "OTHER"),
        ("CREATE TABLE secret_name (a int)", "OTHER"),
        ("", "OTHER"),
    ],
)
def test_sql_operation_is_a_fixed_vocabulary_never_the_statement(
    statement: str, operation: str
) -> None:
    from app.core.telemetry import sql_operation

    assert sql_operation(statement) == operation
