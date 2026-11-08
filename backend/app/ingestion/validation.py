"""Synchronous, independent PDF validation checks (run in a worker thread by the API)."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from typing import BinaryIO

from pypdf import PdfReader

from app.core.config import Settings
from app.core.exceptions import (
    DocumentEncryptedError,
    DocumentTooManyPagesError,
    DocumentValidationError,
    PayloadTooLargeError,
    UnsupportedMediaTypeError,
)

_ALLOWED_MIME = frozenset(
    {"application/pdf", "application/x-pdf", "application/acrobat", "application/octet-stream", ""}
)
_MAGIC = b"%PDF-"
_MAGIC_WINDOW = 1024  # the PDF spec tolerates junk before the header within the first 1 KiB
_CHUNK = 1024 * 1024


@dataclass(frozen=True, slots=True)
class ValidatedPdf:
    size: int
    sha256: str
    page_count: int


def safe_filename(raw: str | None) -> str:
    """Display name only (never used for paths): basename, no control chars, bounded length."""
    name = (raw or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(ch for ch in name if ch.isprintable() and ch not in '<>:"|?*').strip()
    return name[:255]


def validate_pdf(
    file: BinaryIO, *, filename: str | None, content_type: str | None, settings: Settings
) -> ValidatedPdf:
    """Run every upload check in order; raises a specific AppError for each failure."""
    # 1-2. presence and extension (the extension alone never makes a file valid)
    name = safe_filename(filename)
    if not name:
        raise DocumentValidationError("The upload has no file name.", code="DOCUMENT_MISSING")
    if not name.lower().endswith(".pdf"):
        raise UnsupportedMediaTypeError(
            "Only PDF files are supported (the file name must end in .pdf).",
            code="DOCUMENT_UNSUPPORTED_TYPE",
        )
    # 3. declared MIME type
    declared = (content_type or "").split(";", 1)[0].strip().lower()
    if declared not in _ALLOWED_MIME:
        raise UnsupportedMediaTypeError(
            "The declared content type is not PDF.", code="DOCUMENT_UNSUPPORTED_TYPE"
        )
    # 7. size
    file.seek(0, os.SEEK_END)
    size = file.tell()
    file.seek(0)
    if size == 0:
        raise DocumentValidationError("The uploaded file is empty.", code="DOCUMENT_EMPTY")
    if size > settings.max_pdf_bytes:
        raise PayloadTooLargeError(
            f"The PDF is larger than the {settings.max_pdf_bytes // (1024 * 1024)} MiB limit.",
            code="DOCUMENT_TOO_LARGE",
        )
    # 4. magic bytes
    if _MAGIC not in file.read(_MAGIC_WINDOW):
        raise DocumentValidationError(
            "The file content is not a PDF (missing %PDF- header).", code="DOCUMENT_INVALID_FORMAT"
        )
    file.seek(0)
    # content hash (dedup key) computed over the original bytes
    digest = hashlib.sha256()
    while chunk := file.read(_CHUNK):
        digest.update(chunk)
    file.seek(0)
    # 5-6, 8-9. parser validity, readability, page count, encryption
    page_count = _inspect_with_parser(file, settings)
    file.seek(0)
    return ValidatedPdf(size=size, sha256=digest.hexdigest(), page_count=page_count)


def _inspect_with_parser(file: BinaryIO, settings: Settings) -> int:
    try:
        reader = PdfReader(file, strict=False)
        encrypted = reader.is_encrypted
        page_count = 0 if encrypted else len(reader.pages)
    except Exception as exc:
        # pypdf raises many unrelated exception types (PdfReadError, ValueError, KeyError,
        # RecursionError, ...) on hostile or damaged input; every one means "not a usable PDF".
        raise DocumentValidationError(
            "The PDF is corrupted or malformed and cannot be read.", code="DOCUMENT_CORRUPTED"
        ) from exc
    if encrypted:
        raise DocumentEncryptedError()
    if page_count == 0:
        raise DocumentValidationError("The PDF contains no pages.", code="DOCUMENT_CORRUPTED")
    if page_count > settings.max_pdf_pages:
        raise DocumentTooManyPagesError(
            f"The PDF has {page_count} pages; the maximum is {settings.max_pdf_pages}."
        )
    return page_count
