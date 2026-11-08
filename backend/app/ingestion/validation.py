"""Synchronous, independent PDF validation checks (run in a worker thread by the API)."""

from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from app.core.config import Settings
from app.core.exceptions import (
    DocumentEncryptedError,
    DocumentTooManyPagesError,
    DocumentValidationError,
    PayloadTooLargeError,
    UnsupportedMediaTypeError,
)
from app.ingestion.parsing import ParseError, PdfParser, SandboxedPdfParser

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
    file: BinaryIO,
    *,
    filename: str | None,
    content_type: str | None,
    settings: Settings,
    parser: PdfParser | None = None,
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
    page_count = _inspect_in_sandbox(file, settings, parser or SandboxedPdfParser(settings))
    file.seek(0)
    return ValidatedPdf(size=size, sha256=digest.hexdigest(), page_count=page_count)


def _inspect_in_sandbox(file: BinaryIO, settings: Settings, parser: PdfParser) -> int:
    """Open the file with the real parser in a resource-limited child process."""
    with tempfile.TemporaryDirectory(prefix="upload-") as directory:
        path = Path(directory) / "upload.pdf"
        with path.open("wb") as out:
            while chunk := file.read(_CHUNK):
                out.write(chunk)
        try:
            inspected = parser.inspect(path)
        except ParseError as exc:
            if exc.code == "DOCUMENT_PARSE_TIMEOUT":
                raise DocumentValidationError(
                    "The PDF took too long to inspect and was rejected.", code=exc.code
                ) from exc
            raise DocumentValidationError(
                "The PDF is corrupted or malformed and cannot be read.", code="DOCUMENT_CORRUPTED"
            ) from exc
    if inspected.encrypted:
        raise DocumentEncryptedError()
    if inspected.page_count == 0:
        raise DocumentValidationError("The PDF contains no pages.", code="DOCUMENT_CORRUPTED")
    if inspected.page_count > settings.max_pdf_pages:
        raise DocumentTooManyPagesError(
            f"The PDF has {inspected.page_count} pages; the maximum is {settings.max_pdf_pages}."
        )
    return inspected.page_count
