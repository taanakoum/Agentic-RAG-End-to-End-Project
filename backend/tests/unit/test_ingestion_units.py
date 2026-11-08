from __future__ import annotations

import hashlib
import io
import uuid
from itertools import pairwise
from typing import Any

import pytest

from app.core.config import Settings
from app.core.exceptions import (
    DocumentEncryptedError,
    DocumentTooManyPagesError,
    DocumentValidationError,
    PayloadTooLargeError,
    UnsupportedMediaTypeError,
)
from app.ingestion.indexing import point_id
from app.ingestion.states import (
    ALLOWED,
    DocumentStatus,
    IllegalTransitionError,
    check_transition,
    is_retrievable,
)
from app.ingestion.textnorm import normalize_text
from app.ingestion.validation import safe_filename, validate_pdf
from tests.pdfs import (
    corrupted_pdf,
    encrypted_pdf,
    make_pdf,
    sample_text_pdf,
    truncated_pdf,
)

S = DocumentStatus


def settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "app_env": "dev",
        "jwt_secret": "x" * 40,
        "mfa_encryption_key": "dGVzdC1vbmx5LWZlcm5ldC1rZXktMzItYnl0ZXMhISE=",
    }
    return Settings.build(**{**base, **overrides})


def check(data: bytes, *, name: str = "doc.pdf", mime: str | None = "application/pdf", **kw: Any):
    return validate_pdf(io.BytesIO(data), filename=name, content_type=mime, settings=settings(**kw))


# ================================================================ validation (sandboxed inspect)


def test_valid_pdf_passes_and_is_hashed() -> None:
    data = sample_text_pdf()
    result = check(data)
    assert result.page_count == 2
    assert result.size == len(data)
    assert result.sha256 == hashlib.sha256(data).hexdigest()


def test_stream_position_is_reset_for_the_caller() -> None:
    buffer = io.BytesIO(sample_text_pdf())
    validate_pdf(buffer, filename="a.pdf", content_type="application/pdf", settings=settings())
    assert buffer.tell() == 0


def _code(exc_info: pytest.ExceptionInfo[Any]) -> str:
    return str(exc_info.value.code)


def test_empty_file() -> None:
    with pytest.raises(DocumentValidationError) as e:
        check(b"")
    assert _code(e) == "DOCUMENT_EMPTY"


def test_renamed_non_pdf_is_rejected_by_content_not_name() -> None:
    with pytest.raises(DocumentValidationError) as e:
        check(b"just some plain text pretending to be a pdf")
    assert _code(e) == "DOCUMENT_INVALID_FORMAT"
    assert e.value.status_code == 400


@pytest.mark.parametrize("name", ["doc.exe", "doc.pdf.exe", "doc", "doc.png", "doc.doc"])
def test_wrong_extension_is_unsupported_even_with_pdf_content(name: str) -> None:
    with pytest.raises(UnsupportedMediaTypeError) as e:
        check(sample_text_pdf(), name=name)
    assert e.value.status_code == 415


@pytest.mark.parametrize("mime", ["text/plain", "image/png", "application/json"])
def test_wrong_declared_mime_is_unsupported(mime: str) -> None:
    with pytest.raises(UnsupportedMediaTypeError):
        check(sample_text_pdf(), mime=mime)


@pytest.mark.parametrize(
    "mime",
    ["application/pdf", "APPLICATION/PDF; charset=binary", "application/octet-stream", None, ""],
)
def test_acceptable_mime_types(mime: str | None) -> None:
    assert check(sample_text_pdf(), mime=mime).page_count == 2


def test_missing_filename() -> None:
    with pytest.raises(DocumentValidationError) as e:
        check(sample_text_pdf(), name="")
    assert _code(e) == "DOCUMENT_MISSING"


def test_corrupted_and_truncated_pdfs() -> None:
    for data in (corrupted_pdf(), truncated_pdf()):
        with pytest.raises(DocumentValidationError) as e:
            check(data)
        assert _code(e) == "DOCUMENT_CORRUPTED"


def test_encrypted_pdf() -> None:
    with pytest.raises(DocumentEncryptedError) as e:
        check(encrypted_pdf())
    assert e.value.status_code == 422
    assert _code(e) == "DOCUMENT_ENCRYPTED"


def test_oversized_pdf() -> None:
    with pytest.raises(PayloadTooLargeError) as e:
        check(sample_text_pdf(), max_pdf_bytes=1024)
    assert e.value.status_code == 413
    assert _code(e) == "DOCUMENT_TOO_LARGE"


def test_too_many_pages() -> None:
    with pytest.raises(DocumentTooManyPagesError):
        check(make_pdf([f"page {i} has some text" for i in range(6)]), max_pdf_pages=5)


def test_header_may_follow_a_small_prefix_but_not_beyond_1k() -> None:
    data = sample_text_pdf()
    assert check(b"\n\n junk \n" + data).page_count == 2
    with pytest.raises(DocumentValidationError):
        check(b"x" * 2000 + data)


def test_inspection_timeout_becomes_a_clean_rejection() -> None:
    with pytest.raises(DocumentValidationError) as e:
        check(sample_text_pdf(), inspect_timeout_s=0.01)  # the child cannot even start in 10 ms
    assert _code(e) == "DOCUMENT_PARSE_TIMEOUT"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("../../etc/passwd.pdf", "passwd.pdf"),
        ("C:\\Users\\bob\\report.pdf", "report.pdf"),
        ("a\x00b\nc.pdf", "abc.pdf"),
        ("<script>.pdf", "script.pdf"),
        (None, ""),
        ("x" * 400 + ".pdf", ("x" * 400 + ".pdf")[:255]),
    ],
)
def test_safe_filename(raw: str | None, expected: str) -> None:
    assert safe_filename(raw) == expected


# ================================================================ state machine


def test_happy_path_is_allowed_step_by_step() -> None:
    path = [
        S.PENDING, S.VALIDATING, S.QUEUED, S.PROCESSING, S.EXTRACTING,
        S.CHUNKING, S.EMBEDDING, S.INDEXING, S.COMPLETED,
    ]  # fmt: skip
    for current, target in pairwise(path):
        check_transition(current, target)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (S.PENDING, S.COMPLETED),
        (S.QUEUED, S.COMPLETED),
        (S.QUEUED, S.INDEXING),
        (S.EXTRACTING, S.EMBEDDING),
        (S.INDEXING, S.EXTRACTING),
        (S.COMPLETED, S.FAILED),
        (S.COMPLETED, S.PROCESSING),
        (S.FAILED, S.COMPLETED),
        (S.FAILED, S.PROCESSING),
        (S.COMPLETED, S.COMPLETED),
    ],
)
def test_illegal_transitions_raise(current: DocumentStatus, target: DocumentStatus) -> None:
    with pytest.raises(IllegalTransitionError):
        check_transition(current, target)


def test_every_active_stage_can_fail_or_requeue_and_failed_only_requeues() -> None:
    for stage in (S.PROCESSING, S.EXTRACTING, S.CHUNKING, S.EMBEDDING, S.INDEXING):
        assert {S.FAILED, S.QUEUED} <= ALLOWED[stage]
    assert ALLOWED[S.FAILED] == {S.QUEUED}
    assert ALLOWED[S.COMPLETED] == {S.QUEUED}


def test_only_completed_is_retrievable() -> None:
    assert [s for s in S if is_retrievable(s)] == [S.COMPLETED]


# ================================================================ text helpers


def test_normalize_text() -> None:
    assert normalize_text("a\u00a0\u00a0b \t c\n\n\n\n\nd\x00e") == "a b c\n\nde"
    assert normalize_text("0a 9z") == "0a 9z"  # digits/letters are never stripped


def test_point_ids_are_deterministic_and_distinct() -> None:
    doc = uuid.uuid4()
    assert point_id(doc, 1, 0) == point_id(doc, 1, 0)
    ids = {point_id(doc, v, i) for v in (1, 2) for i in range(5)}
    assert len(ids) == 10
    assert point_id(uuid.uuid4(), 1, 0) != point_id(doc, 1, 0)
