"""PDF parsing + chunking orchestration. All untrusted-bytes parsing happens in the sandbox.

Two interchangeable pipelines (setting `PDF_PIPELINE`):
  * docling (default): Docling layout analysis + HybridChunker in the sandboxed child. Understands
    headings by layout (not just text), reading order and tables, and OCRs only when a page has no
    text layer. Chunks come back with page provenance and heading paths.
  * light: pypdf text layer (+ RapidOCR for scanned pages) in the child, then repair and
    sentence-aware chunking in this process. ~100x faster, much lighter, weaker on structure.
If Docling crashes on a file (not a timeout or memory kill) the light pipeline is tried instead.
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol

from app.core.config import Settings
from app.core.logging import get_logger
from app.ingestion.chunking import Chunk, chunk_pages, contextual_text, estimate_tokens
from app.ingestion.sandbox import SandboxError, SandboxLimits, module_command, run_sandboxed
from app.ingestion.textclean import clean_pages
from app.ingestion.textnorm import PageText, meaningful_chars

log = get_logger(__name__)

_MODULE = "app.ingestion.sandbox_jobs"


class ParseError(Exception):
    """The document cannot be parsed; `code` is a stable machine-readable reason."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class InspectResult:
    encrypted: bool
    page_count: int


@dataclass(frozen=True, slots=True)
class ParsedDocument:
    chunks: list[Chunk]
    page_count: int
    ocr_status: str
    ocr_pages: int
    parser: str  # "docling" | "light"


class PdfParser(Protocol):
    def inspect(self, path: Path) -> InspectResult: ...

    def parse(self, path: Path, *, title: str, page_count: int) -> ParsedDocument: ...


def _error_from_sandbox(exc: SandboxError) -> ParseError:
    if exc.kind == "timeout":
        return ParseError("DOCUMENT_PARSE_TIMEOUT", "The document took too long to process.")
    if exc.kind == "memory":
        return ParseError("DOCUMENT_PARSE_MEMORY", "The document needs more memory than allowed.")
    return ParseError("DOCUMENT_PARSE_FAILED", "The document could not be processed safely.")


class SandboxedPdfParser:
    def __init__(self, settings: Settings) -> None:
        self._s = settings

    # ------------------------------------------------------------------ inspect (cheap)

    def inspect(self, path: Path) -> InspectResult:
        s = self._s
        argv, env = module_command(_MODULE, "inspect", path, "{}")
        try:
            result = run_sandboxed(
                argv,
                limits=SandboxLimits(timeout_s=s.inspect_timeout_s, memory_mb=s.inspect_memory_mb),
                extra_env=env,
            )
        except SandboxError as exc:
            raise _error_from_sandbox(exc) from exc
        if not result.get("ok"):
            raise ParseError(
                str(result.get("code", "DOCUMENT_CORRUPTED")), str(result.get("message", ""))
            )
        return InspectResult(bool(result["encrypted"]), int(result["page_count"]))

    # ------------------------------------------------------------------ parse + chunk

    def _options(self) -> dict[str, Any]:
        s = self._s
        return {
            "min_text_chars_per_page": s.min_text_chars_per_page,
            "ocr_enabled": s.ocr_enabled,
            "ocr_max_pages": s.ocr_max_pages,
            "ocr_dpi": s.ocr_dpi,
            "docling_tables": s.docling_tables,
            "tokenizer": s.embedding_model,
            "max_tokens": s.docling_max_tokens,
        }

    def _run(self, command: str, path: Path, page_count: int) -> dict[str, Any]:
        s = self._s
        timeout = min(
            s.parse_timeout_max_s, s.parse_timeout_base_s + s.parse_timeout_per_page_s * page_count
        )
        argv, env = module_command(_MODULE, command, path, json.dumps(self._options()))
        result = run_sandboxed(
            argv,
            limits=SandboxLimits(timeout_s=timeout, memory_mb=s.parse_memory_mb),
            extra_env=env,
        )
        if not result.get("ok"):
            raise ParseError(
                str(result.get("code", "DOCUMENT_CORRUPTED")), str(result.get("message", ""))
            )
        return result

    def parse(self, path: Path, *, title: str, page_count: int) -> ParsedDocument:
        if self._s.pdf_pipeline == "docling":
            try:
                return self._parse_docling(path, title, page_count)
            except SandboxError as exc:
                if exc.kind in {"timeout", "memory"}:
                    raise _error_from_sandbox(exc) from exc
                log.warning("docling_failed_falling_back", kind=exc.kind)
        try:
            return self._parse_light(path, title, page_count)
        except SandboxError as exc:
            raise _error_from_sandbox(exc) from exc

    def _parse_docling(self, path: Path, title: str, page_count: int) -> ParsedDocument:
        result = self._run("docling", path, page_count)
        chunks: list[Chunk] = []
        for raw in result["chunks"]:
            text = str(raw["text"]).strip()
            alnum = meaningful_chars(text)
            if alnum < self._s.min_chunk_chars or alnum / max(len(text), 1) < 0.3:
                continue
            pages = [int(p) for p in raw["pages"]] or [1]
            headings = [str(h) for h in raw["headings"]]
            chunks.append(
                Chunk(
                    index=len(chunks),
                    page_number=min(pages),
                    page_end=max(pages),
                    heading=headings[-1] if headings else None,
                    section_path=" > ".join(headings),
                    text=text,
                    tokens=estimate_tokens(text),
                    embed_text=f"{title}\n{raw['contextual']}",
                )
            )
        if result.get("fallback_pages"):
            # pages Docling recognised text on but did not chunk: chunk them ourselves, then
            # restore reading order and renumber
            extra = self._chunk_text_pages(result["fallback_pages"], title)
            merged = sorted([*chunks, *extra], key=lambda c: (c.page_number, c.index))
            chunks = [replace(c, index=i) for i, c in enumerate(merged)]
        return ParsedDocument(
            chunks,
            int(result["page_count"]),
            str(result["ocr_status"]),
            int(result["ocr_pages"]),
            "docling",
        )

    def _chunk_text_pages(self, raw_pages: list[dict[str, Any]], title: str) -> list[Chunk]:
        s = self._s
        pages = [
            PageText(int(p["page_number"]), str(p["text"]), bool(p["from_ocr"])) for p in raw_pages
        ]
        chunks = chunk_pages(
            clean_pages(pages),
            target_tokens=s.chunk_target_tokens,
            max_tokens=s.chunk_max_tokens,
            min_tokens=s.chunk_min_tokens,
            overlap_tokens=s.chunk_overlap_tokens,
            min_chars=s.min_chunk_chars,
        )
        return [replace(c, embed_text=contextual_text(c, title=title)) for c in chunks]

    def _parse_light(self, path: Path, title: str, page_count: int) -> ParsedDocument:
        s = self._s
        result = self._run("light", path, page_count)
        pages = [
            PageText(int(p["page_number"]), str(p["text"]), bool(p["from_ocr"]))
            for p in result["pages"]
        ]
        chunks = chunk_pages(
            clean_pages(pages),
            target_tokens=s.chunk_target_tokens,
            max_tokens=s.chunk_max_tokens,
            min_tokens=s.chunk_min_tokens,
            overlap_tokens=s.chunk_overlap_tokens,
            min_chars=s.min_chunk_chars,
        )
        chunks = [replace(c, embed_text=contextual_text(c, title=title)) for c in chunks]
        return ParsedDocument(
            chunks,
            int(result["page_count"]),
            str(result["ocr_status"]),
            int(result["ocr_pages"]),
            "light",
        )


def parse_bytes(parser: PdfParser, data: bytes, *, title: str, page_count: int) -> ParsedDocument:
    """Write the PDF to a private temp file, parse it, delete it."""
    with tempfile.TemporaryDirectory(prefix="pdf-") as directory:
        path = Path(directory) / "document.pdf"
        path.write_bytes(data)
        return parser.parse(path, title=title, page_count=page_count)
