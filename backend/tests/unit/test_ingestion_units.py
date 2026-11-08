from __future__ import annotations

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
from app.ingestion.chunking import chunk_pages, looks_like_heading
from app.ingestion.extraction import (
    PageText,
    extract_text_layer,
    extract_with_ocr_fallback,
    normalize_text,
)
from app.ingestion.indexing import point_id
from app.ingestion.states import (
    ALLOWED,
    DocumentStatus,
    IllegalTransitionError,
    check_transition,
    is_retrievable,
)
from app.ingestion.validation import safe_filename, validate_pdf
from tests.pdfs import (
    blank_pdf,
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


# ================================================================ validation


def test_valid_pdf_passes_and_is_hashed() -> None:
    data = sample_text_pdf()
    result = check(data)
    assert result.page_count == 2
    assert result.size == len(data)
    import hashlib

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


@pytest.mark.parametrize("name", ["doc.txt", "doc.pdf.exe", "doc", "doc.docx"])
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
        S.PENDING,
        S.VALIDATING,
        S.QUEUED,
        S.PROCESSING,
        S.EXTRACTING,
        S.CHUNKING,
        S.EMBEDDING,
        S.INDEXING,
        S.COMPLETED,
    ]
    for current, target in pairwise(path):
        check_transition(current, target)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (S.PENDING, S.COMPLETED),
        (S.QUEUED, S.COMPLETED),
        (S.QUEUED, S.INDEXING),
        (S.EXTRACTING, S.EMBEDDING),  # skipping a stage
        (S.INDEXING, S.EXTRACTING),  # going backwards
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
    assert ALLOWED[S.COMPLETED] == {S.QUEUED}  # explicit re-index only


def test_only_completed_is_retrievable() -> None:
    assert [s for s in S if is_retrievable(s)] == [S.COMPLETED]


# ================================================================ extraction / chunking


def test_text_layer_extraction_keeps_pages() -> None:
    pages, total = extract_text_layer(io.BytesIO(sample_text_pdf("zeta")), settings=settings())
    assert total == 2
    assert [p.page_number for p in pages] == [1, 2]
    assert "quarterly results" in pages[0].text
    assert "zeta" in pages[1].text
    assert "financial summary" in pages[1].text.lower()


def test_normalize_text() -> None:
    assert normalize_text("a\u00a0\u00a0b \t c\n\n\n\n\nd\x00e") == "a b c\n\nde"
    assert normalize_text("0a 9z") == "0a 9z"  # digits/letters are never stripped


class FakeOcr:
    def __init__(self, text: str = "Recovered text from the scanned page number") -> None:
        self.calls: list[int] = []
        self.text = text

    def ocr_pdf_page(self, pdf_bytes: bytes, page_index: int, *, dpi: int) -> str:
        self.calls.append(page_index)
        return f"{self.text} {page_index + 1}"


def test_ocr_runs_only_for_pages_without_text() -> None:
    pages = [
        PageText(1, "A page that already has plenty of extractable text in its layer."),
        PageText(2, ""),
        PageText(3, "   12  "),
    ]
    ocr = FakeOcr()
    result = extract_with_ocr_fallback(b"pdf", pages, settings=settings(), ocr=ocr)
    assert ocr.calls == [1, 2]
    assert result.ocr_status == "used"
    assert result.ocr_pages == 2
    assert [p.from_ocr for p in result.pages] == [False, True, True]
    assert result.pages[0].text == pages[0].text


def test_ocr_not_needed_when_text_is_present() -> None:
    pages = [PageText(1, "Plenty of real text on this page, definitely enough characters.")]
    ocr = FakeOcr()
    result = extract_with_ocr_fallback(b"pdf", pages, settings=settings(), ocr=ocr)
    assert (result.ocr_status, result.ocr_pages, ocr.calls) == ("not_needed", 0, [])


def test_ocr_disabled_or_missing_engine() -> None:
    pages = [PageText(1, "")]
    assert (
        extract_with_ocr_fallback(
            b"x", pages, settings=settings(ocr_enabled=False), ocr=FakeOcr()
        ).ocr_status
        == "disabled"
    )
    assert (
        extract_with_ocr_fallback(b"x", pages, settings=settings(), ocr=None).ocr_status
        == "disabled"
    )


def test_ocr_page_limit_marks_result_limited() -> None:
    pages = [PageText(i, "") for i in range(1, 6)]
    ocr = FakeOcr()
    result = extract_with_ocr_fallback(b"x", pages, settings=settings(ocr_max_pages=2), ocr=ocr)
    assert ocr.calls == [0, 1]
    assert result.ocr_status == "limited"
    assert result.ocr_pages == 2


def test_ocr_failure_on_every_page_is_reported_not_raised() -> None:
    class Broken:
        def ocr_pdf_page(self, pdf_bytes: bytes, page_index: int, *, dpi: int) -> str:
            raise RuntimeError("model failed")

    result = extract_with_ocr_fallback(
        b"x", [PageText(1, ""), PageText(2, "")], settings=settings(), ocr=Broken()
    )
    assert result.ocr_status == "failed"
    assert result.ocr_pages == 0


def test_blank_pdf_has_no_text_layer() -> None:
    pages, total = extract_text_layer(io.BytesIO(blank_pdf()), settings=settings())
    assert total == 1
    assert pages[0].text == ""


def long_text(n_sentences: int) -> str:
    return " ".join(
        f"Sentence number {i} talks about subject {i % 7} in some detail."
        for i in range(n_sentences)
    )


def test_chunks_respect_size_and_page_boundaries_and_overlap() -> None:
    pages = [PageText(1, long_text(60)), PageText(2, long_text(30))]
    chunks = chunk_pages(pages, size=400, overlap=80, min_chars=20)
    assert chunks
    assert all(len(c.text) <= 400 for c in chunks)
    assert [c.index for c in chunks] == list(range(len(chunks)))
    assert {c.page_number for c in chunks} == {1, 2}
    page1 = [c for c in chunks if c.page_number == 1]
    assert page1[-1].index < min(c.index for c in chunks if c.page_number == 2)
    # overlap: consecutive chunks of a page share some text
    shared = [a.text[-40:] in b.text or b.text[:40] in a.text for a, b in pairwise(page1)]
    assert any(shared)


def test_every_sentence_ends_up_in_some_chunk() -> None:
    text = long_text(40)
    chunks = chunk_pages([PageText(1, text)], size=300, overlap=50, min_chars=20)
    joined = " ".join(c.text for c in chunks)
    for i in range(40):
        assert f"Sentence number {i} " in joined


def test_headings_are_tracked_across_pages() -> None:
    pages = [
        PageText(1, "INTRODUCTION\nThis is the first section with enough words to make a chunk."),
        PageText(
            2, "More text continuing the same section on the next page, still no new heading."
        ),
        PageText(3, "2.1 Methods\nA description of the methods used in the study goes here."),
    ]
    chunks = chunk_pages(pages, size=500, overlap=50, min_chars=20)
    assert [c.heading for c in chunks] == ["INTRODUCTION", "INTRODUCTION", "2.1 Methods"]


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("INTRODUCTION", True),
        ("1.2 Related Work", True),
        ("Financial Summary", True),
        ("This is a normal sentence that ends with a period.", False),
        ("a" * 120, False),
        ("see the table below,", False),
        ("ok", False),
    ],
)
def test_heading_detection(line: str, expected: bool) -> None:
    assert looks_like_heading(line) is expected


def test_meaningless_chunks_are_dropped() -> None:
    pages = [
        PageText(1, "- - - - - - - - - - - - - - - - - - - - - - - - - - - - - -"),
        PageText(2, "12"),
        PageText(3, ""),
    ]
    assert chunk_pages(pages, size=500, overlap=50, min_chars=30) == []


def test_unbroken_text_is_hard_split_within_size() -> None:
    chunks = chunk_pages([PageText(1, "x" * 2500)], size=500, overlap=0, min_chars=10)
    assert all(len(c.text) <= 500 for c in chunks)
    assert sum(len(c.text) for c in chunks) == 2500


# ================================================================ deterministic point ids


def test_point_ids_are_deterministic_and_distinct() -> None:
    doc = uuid.uuid4()
    assert point_id(doc, 1, 0) == point_id(doc, 1, 0)
    ids = {point_id(doc, v, i) for v in (1, 2) for i in range(5)}
    assert len(ids) == 10
    assert point_id(uuid.uuid4(), 1, 0) != point_id(doc, 1, 0)
