"""Text extraction from PDFs (pypdf text layer) with an open-source OCR fallback."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import BinaryIO, Protocol

from pypdf import PdfReader

from app.core.config import Settings
from app.core.exceptions import DocumentParsingError
from app.core.logging import get_logger

log = get_logger(__name__)

_NBSP = chr(0xA0)  # built from the code point so the source has no ambiguous literal
_WS = re.compile(rf"[ \t\f\v{_NBSP}]+")
_BLANKLINES = re.compile(r"\n{3,}")


@dataclass(frozen=True, slots=True)
class PageText:
    page_number: int  # 1-based
    text: str
    from_ocr: bool = False


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    pages: list[PageText]
    page_count: int
    ocr_status: str  # not_needed | used | failed | disabled | limited
    ocr_pages: int


class OcrEngine(Protocol):
    """Renders and recognises one page of a PDF. Implementations: RapidOCR (see ocr.py)."""

    def ocr_pdf_page(self, pdf_bytes: bytes, page_index: int, *, dpi: int) -> str: ...


def normalize_text(raw: str) -> str:
    text = unicodedata.normalize("NFKC", raw).replace("\x00", "")
    text = "\n".join(_WS.sub(" ", line).strip() for line in text.splitlines())
    return _BLANKLINES.sub("\n\n", text).strip()


def meaningful_chars(text: str) -> int:
    return sum(1 for ch in text if ch.isalnum())


def extract_text_layer(stream: BinaryIO, *, settings: Settings) -> tuple[list[PageText], int]:
    """Per-page text from the PDF text layer. A page that cannot be read yields empty text."""
    try:
        reader = PdfReader(stream, strict=False)
        if reader.is_encrypted:
            raise DocumentParsingError("The PDF is encrypted.", code="DOCUMENT_ENCRYPTED")
        total = len(reader.pages)
    except DocumentParsingError:
        raise
    except Exception as exc:
        # pypdf raises many unrelated exception types on damaged input.
        raise DocumentParsingError(
            "The PDF could not be parsed.", code="DOCUMENT_CORRUPTED"
        ) from exc
    pages: list[PageText] = []
    for index in range(total):
        try:
            raw = reader.pages[index].extract_text() or ""
        except Exception:
            # One unreadable page must not fail the whole document (OCR may still recover it).
            log.warning("page_text_extraction_failed", page=index + 1)
            raw = ""
        pages.append(PageText(page_number=index + 1, text=normalize_text(raw)))
    return pages, total


def extract_with_ocr_fallback(
    pdf_bytes: bytes, pages: list[PageText], *, settings: Settings, ocr: OcrEngine | None
) -> ExtractionResult:
    """Replace pages that have (almost) no text layer by OCR output, within configured limits."""
    needs_ocr = [
        p.page_number for p in pages if meaningful_chars(p.text) < settings.min_text_chars_per_page
    ]
    if not needs_ocr:
        return ExtractionResult(pages, len(pages), "not_needed", 0)
    if not settings.ocr_enabled or ocr is None:
        return ExtractionResult(pages, len(pages), "disabled", 0)

    limited = len(needs_ocr) > settings.ocr_max_pages
    targets = needs_ocr[: settings.ocr_max_pages]
    result = {p.page_number: p for p in pages}
    recognised = 0
    failures = 0
    for number in targets:
        try:
            text = normalize_text(ocr.ocr_pdf_page(pdf_bytes, number - 1, dpi=settings.ocr_dpi))
        except Exception:
            # OCR engines raise assorted errors (bad image, model, memory); count and continue.
            log.warning("ocr_page_failed", page=number)
            failures += 1
            continue
        if meaningful_chars(text) >= settings.min_text_chars_per_page:
            result[number] = PageText(page_number=number, text=text, from_ocr=True)
            recognised += 1
    if recognised == 0 and failures == len(targets):
        status = "failed"
    elif limited:
        status = "limited"
    else:
        status = "used"
    ordered = [result[p.page_number] for p in pages]
    return ExtractionResult(ordered, len(pages), status, recognised)
