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

_OCTET = ("application/octet-stream", "")
#: extension -> (format, allowed declared MIME types, mime type stored on the document)
FORMATS: dict[str, tuple[str, frozenset[str], str]] = {
    ".pdf": (
        "pdf",
        frozenset({"application/pdf", "application/x-pdf", "application/acrobat", *_OCTET}),
        "application/pdf",
    ),
    ".docx": (
        "docx",
        frozenset(
            {
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "application/zip",
                *_OCTET,
            }
        ),
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ),
    ".txt": ("txt", frozenset({"text/plain", *_OCTET}), "text/plain"),
    ".md": (
        "md",
        frozenset({"text/markdown", "text/x-markdown", "text/plain", *_OCTET}),
        "text/markdown",
    ),
    ".markdown": (
        "md",
        frozenset({"text/markdown", "text/x-markdown", "text/plain", *_OCTET}),
        "text/markdown",
    ),
    ".csv": (
        "csv",
        frozenset(
            {"text/csv", "application/csv", "application/vnd.ms-excel", "text/plain", *_OCTET}
        ),
        "text/csv",
    ),
}
SUPPORTED_EXTENSIONS = "PDF, DOCX, TXT, MD, CSV"
_MAGIC = b"%PDF-"
_ZIP_MAGIC = b"PK\x03\x04"
_MAGIC_WINDOW = 1024  # the PDF spec tolerates junk before the header within the first 1 KiB
_CHUNK = 1024 * 1024


@dataclass(frozen=True, slots=True)
class ValidatedPdf:
    size: int
    sha256: str
    page_count: int
    source_format: str = "pdf"
    mime_type: str = "application/pdf"


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
    """Run every upload check in order; raises a specific AppError for each failure.

    Despite the historical name this validates every supported format (PDF, DOCX, TXT, MD, CSV).
    """
    # 1-2. presence and extension (the extension alone never makes a file valid)
    name = safe_filename(filename)
    if not name:
        raise DocumentValidationError("The upload has no file name.", code="DOCUMENT_MISSING")
    extension = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if extension not in FORMATS:
        raise UnsupportedMediaTypeError(
            f"Unsupported file type. Supported: {SUPPORTED_EXTENSIONS}.",
            code="DOCUMENT_UNSUPPORTED_TYPE",
        )
    source_format, allowed_mime, stored_mime = FORMATS[extension]
    # 3. declared MIME type
    declared = (content_type or "").split(";", 1)[0].strip().lower()
    if declared not in allowed_mime:
        raise UnsupportedMediaTypeError(
            f"The declared content type does not match a {source_format.upper()} file.",
            code="DOCUMENT_UNSUPPORTED_TYPE",
        )
    # 7. size (per format family)
    limit = settings.max_pdf_bytes if source_format in {"pdf", "docx"} else settings.max_text_bytes
    file.seek(0, os.SEEK_END)
    size = file.tell()
    file.seek(0)
    if size == 0:
        raise DocumentValidationError("The uploaded file is empty.", code="DOCUMENT_EMPTY")
    if size > limit:
        raise PayloadTooLargeError(
            f"The file is larger than the {limit // (1024 * 1024)} MiB limit for this type.",
            code="DOCUMENT_TOO_LARGE",
        )
    # 4. magic bytes (binary formats; text formats are checked by decoding in the sandbox)
    head = file.read(_MAGIC_WINDOW)
    if source_format == "pdf" and _MAGIC not in head:
        raise DocumentValidationError(
            "The file content is not a PDF (missing %PDF- header).", code="DOCUMENT_INVALID_FORMAT"
        )
    if source_format == "docx" and not head.startswith(_ZIP_MAGIC):
        raise DocumentValidationError(
            "The file content is not a Word document.", code="DOCUMENT_INVALID_FORMAT"
        )
    file.seek(0)
    digest = hashlib.sha256()
    while chunk := file.read(_CHUNK):
        digest.update(chunk)
    file.seek(0)
    # 5-6, 8-9. parser validity, readability, page count, encryption, format-specific safety
    page_count = _inspect_in_sandbox(
        file, settings, parser or SandboxedPdfParser(settings), source_format
    )
    file.seek(0)
    return ValidatedPdf(
        size=size,
        sha256=digest.hexdigest(),
        page_count=page_count,
        source_format=source_format,
        mime_type=stored_mime,
    )


def _inspect_in_sandbox(
    file: BinaryIO, settings: Settings, parser: PdfParser, source_format: str
) -> int:
    """Open the file with the real parser in a resource-limited child process."""
    with tempfile.TemporaryDirectory(prefix="upload-") as directory:
        path = Path(directory) / f"upload.{source_format}"
        with path.open("wb") as out:
            while chunk := file.read(_CHUNK):
                out.write(chunk)
        try:
            inspected = parser.inspect(path, source_format)
        except ParseError as exc:
            if exc.code == "DOCUMENT_PARSE_TIMEOUT":
                raise DocumentValidationError(
                    "The PDF took too long to inspect and was rejected.", code=exc.code
                ) from exc
            if exc.code.startswith("DOCUMENT_") and exc.code not in {
                "DOCUMENT_PARSE_FAILED",
                "DOCUMENT_PARSE_MEMORY",
                "DOCUMENT_CORRUPTED",
            }:
                raise DocumentValidationError(
                    exc.message, code=exc.code
                ) from exc  # format-specific
            raise DocumentValidationError(
                "The file is corrupted or malformed and cannot be read.", code="DOCUMENT_CORRUPTED"
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
