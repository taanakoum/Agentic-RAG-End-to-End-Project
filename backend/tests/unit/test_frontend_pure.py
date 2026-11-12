"""Error wording and formatting helpers of the Streamlit frontend (no server needed)."""

from __future__ import annotations

import httpx
import pytest

from frontend.api import ApiError, _error_from  # pyright: ignore[reportPrivateUsage]
from frontend.errors import describe, stream_error
from frontend.format import (
    answer_notices,
    citation_line,
    has_work_in_flight,
    human_size,
    plural,
    preview_title,
    status_label,
)


def error(code: str, status: int = 400, **kwargs: object) -> ApiError:
    return ApiError(status=status, code=code, message="server text", **kwargs)  # type: ignore[arg-type]


def test_validation_errors_become_field_messages_and_never_echo_values() -> None:
    shown = describe(
        error(
            "VALIDATION_ERROR",
            422,
            details=[
                {"field": "body.email", "code": "x", "message": "Not a valid email."},
                {"field": "body.password", "code": "y", "message": "Too short."},
                {"field": "body.email", "code": "z", "message": "second message ignored"},
            ],
            request_id="rid-1",
        )
    )
    assert shown.kind == "input" and shown.fields == {
        "email": "Not a valid email.",
        "password": "Too short.",
    }
    assert shown.with_reference().endswith("(reference: rid-1)")


@pytest.mark.parametrize(
    ("code", "kind", "needle"),
    [
        ("INVALID_CREDENTIALS", "credentials", "not correct"),
        ("DOCUMENT_INVALID_FORMAT", "document", "not a supported document"),
        ("DOCUMENT_CORRUPTED", "document", "damaged"),
        ("DOCUMENT_ENCRYPTED", "document", "password-protected"),
        ("DOCUMENT_TOO_LARGE", "limit", "larger than the allowed size"),
        ("RATE_LIMIT_EXCEEDED", "limit", "Too many requests"),
        ("MODEL_RATE_LIMITED", "limit", "quota"),
        ("SERVICE_UNAVAILABLE", "outage", "unavailable"),
        ("CONCURRENCY_CONFLICT", "conflict", "still answering"),
        ("SESSION_REVOKED", "auth", "sign in again"),
    ],
)
def test_each_kind_of_failure_has_its_own_wording(code: str, kind: str, needle: str) -> None:
    shown = describe(error(code))
    assert shown.kind == kind and needle in shown.message


def test_retry_is_offered_only_where_repeating_is_safe() -> None:
    assert describe(error("INVALID_CREDENTIALS", 401)).retryable is False
    assert describe(error("DOCUMENT_ENCRYPTED")).retryable is False
    assert describe(error("MODEL_TIMEOUT", 504)).retryable is True
    interrupted = describe(error("STREAM_INTERRUPTED", 503))
    assert interrupted.retryable and interrupted.same_key_retry  # replay under the same key
    failed = describe(error("MODEL_PROVIDER_ERROR", 502))
    assert failed.retryable and not failed.same_key_retry  # a failed turn needs a new key


def test_waiting_time_is_included_when_the_server_gives_one() -> None:
    shown = describe(error("RATE_LIMIT_EXCEEDED", 429, retry_after_s=42))
    assert "about 42 seconds" in shown.message


def test_unknown_codes_use_the_servers_message_and_unknown_5xx_are_outages() -> None:
    assert describe(error("SOMETHING_NEW", 409)).message == "server text"
    assert describe(error("SOMETHING_NEW", 500)).kind == "outage"


def test_an_error_event_inside_a_stream_gets_the_same_wording() -> None:
    shown = stream_error("MODEL_TIMEOUT", "x", True, None)
    assert shown.kind == "outage" and "took too long" in shown.message


def test_a_non_json_error_body_is_never_shown_to_the_user() -> None:
    response = httpx.Response(502, text="<html>Bad gateway: secret upstream detail</html>")
    parsed = _error_from(response)
    assert parsed.code == "UNEXPECTED_RESPONSE" and "secret" not in str(parsed)
    ok = _error_from(
        httpx.Response(
            404,
            json={"error": {"code": "RESOURCE_NOT_FOUND", "message": "m", "request_id": "r"}},
            headers={"retry-after": "7"},
        )
    )
    assert (ok.code, ok.request_id, ok.retry_after_s) == ("RESOURCE_NOT_FOUND", "r", 7)


def test_formatting_helpers() -> None:
    assert human_size(512) == "512 B" and human_size(1536) == "1.5 KB"
    assert human_size(5 * 1024 * 1024) == "5.0 MB"
    assert (plural(1, "page"), plural(3, "page"), plural(0, "passage")) == (
        "1 page",
        "3 pages",
        "0 passages",
    )
    assert status_label("embedding") == "Computing embeddings" and status_label("odd") == "Odd"
    assert has_work_in_flight([{"status": "completed"}, {"status": "queued"}])
    assert not has_work_in_flight([{"status": "completed"}, {"status": "failed"}])
    assert citation_line(2, {"document_name": "a.pdf", "page_number": 3}) == "[2] a.pdf, page 3"
    assert citation_line(1, {"document_name": "a.pdf", "page_number": None}) == "[1] a.pdf"
    assert preview_title("  a   long\n title " * 5, 20).endswith("…")
    assert len(preview_title("x" * 100, 40)) == 40


def test_answer_notices_explain_unverified_insufficient_and_degraded_answers() -> None:
    unverified = answer_notices(
        {"insufficient_evidence": True, "metadata": {"degraded": ["unverified_answer"]}}
    )
    assert [level for level, _ in unverified] == ["warning"]
    insufficient = answer_notices({"insufficient_evidence": True, "metadata": {"degraded": []}})
    assert [level for level, _ in insufficient] == ["info"]
    degraded = answer_notices({"metadata": {"degraded": ["rerank_timeout"]}})
    assert "less precise" in degraded[0][1]
    assert answer_notices({"metadata": {"degraded": []}}) == []
