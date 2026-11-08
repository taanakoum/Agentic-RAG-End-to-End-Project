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
import threading
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol

from app.core.config import Settings
from app.core.logging import get_logger
from app.ingestion.chunking import (
    Block,
    Chunk,
    chunk_blocks,
    chunk_pages,
    contextual_text,
    estimate_tokens,
)
from app.ingestion.sandbox import (
    PersistentSandbox,
    SandboxError,
    SandboxLimits,
    SandboxPool,
    module_command,
    run_sandboxed,
)
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
    """Parses any supported upload (the name is historical; PDF is the default format)."""

    def inspect(self, path: Path, source_format: str = "pdf") -> InspectResult: ...

    def parse(
        self, path: Path, *, title: str, page_count: int, source_format: str = "pdf"
    ) -> ParsedDocument: ...


def _error_from_sandbox(exc: SandboxError) -> ParseError:
    if exc.kind == "timeout":
        return ParseError("DOCUMENT_PARSE_TIMEOUT", "The document took too long to process.")
    if exc.kind == "memory":
        return ParseError("DOCUMENT_PARSE_MEMORY", "The document needs more memory than allowed.")
    return ParseError("DOCUMENT_PARSE_FAILED", "The document could not be processed safely.")


class SandboxedPdfParser:
    """Parses PDFs in sandboxed children. In `pool` mode the children stay warm between calls."""

    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self._lock = threading.Lock()
        self._pools: dict[str, SandboxPool] = {}

    def close(self) -> None:
        with self._lock:
            pools, self._pools = list(self._pools.values()), {}
        for pool in pools:
            pool.close()

    @property
    def spawn_count(self) -> int:
        return sum(pool.spawn_count for pool in self._pools.values())

    def _pool(self, kind: str, *, size: int, memory_mb: int) -> SandboxPool:
        with self._lock:
            pool = self._pools.get(kind)
            if pool is None:
                argv, env = module_command(_MODULE, "serve")
                s = self._s

                def factory() -> PersistentSandbox:
                    return PersistentSandbox(
                        argv,
                        limits=SandboxLimits(timeout_s=s.parse_timeout_max_s, memory_mb=memory_mb),
                        extra_env=env,
                        max_jobs=s.sandbox_recycle_jobs,
                    )

                pool = self._pools[kind] = SandboxPool(factory, size)
            return pool

    def _call(
        self,
        kind: str,
        command: str,
        path: Path,
        opts: dict[str, Any],
        *,
        timeout_s: float,
        memory_mb: int,
        pool_size: int,
    ) -> dict[str, Any]:
        if self._s.sandbox_mode == "pool":
            pool = self._pool(kind, size=pool_size, memory_mb=memory_mb)
            return pool.call(
                {"command": command, "path": str(path), "opts": opts}, timeout_s=timeout_s
            )
        argv, env = module_command(_MODULE, command, path, json.dumps(opts))
        return run_sandboxed(
            argv, limits=SandboxLimits(timeout_s=timeout_s, memory_mb=memory_mb), extra_env=env
        )

    # ------------------------------------------------------------------ inspect (cheap)

    def inspect(self, path: Path, source_format: str = "pdf") -> InspectResult:
        s = self._s
        try:
            result = self._call(
                "inspect",
                "inspect",
                path,
                self._options(source_format),
                timeout_s=s.inspect_timeout_s,
                memory_mb=s.inspect_memory_mb,
                pool_size=s.inspect_pool_size,
            )
        except SandboxError as exc:
            raise _error_from_sandbox(exc) from exc
        if not result.get("ok"):
            raise ParseError(
                str(result.get("code", "DOCUMENT_CORRUPTED")), str(result.get("message", ""))
            )
        return InspectResult(bool(result["encrypted"]), int(result["page_count"]))

    # ------------------------------------------------------------------ parse + chunk

    def _options(self, source_format: str = "pdf") -> dict[str, Any]:
        s = self._s
        return {
            "format": source_format,
            "max_text_bytes": s.max_text_bytes,
            "max_table_rows": s.max_table_rows,
            "max_csv_cols": s.max_csv_cols,
            "docx_max_entries": s.docx_max_entries,
            "docx_max_uncompressed": s.docx_max_uncompressed,
            "docx_max_ratio": s.docx_max_ratio,
            "min_text_chars_per_page": s.min_text_chars_per_page,
            "ocr_enabled": s.ocr_enabled,
            "ocr_max_pages": s.ocr_max_pages,
            "ocr_dpi": s.ocr_dpi,
            "docling_tables": s.docling_tables,
            "tokenizer": s.embedding_model,
            "max_tokens": s.docling_max_tokens,
        }

    def _run(
        self, command: str, path: Path, page_count: int, source_format: str = "pdf"
    ) -> dict[str, Any]:
        s = self._s
        timeout = min(
            s.parse_timeout_max_s, s.parse_timeout_base_s + s.parse_timeout_per_page_s * page_count
        )
        result = self._call(
            "parse",
            command,
            path,
            self._options(source_format),
            timeout_s=timeout,
            memory_mb=s.parse_memory_mb,
            pool_size=s.parse_pool_size,
        )
        if not result.get("ok"):
            raise ParseError(
                str(result.get("code", "DOCUMENT_CORRUPTED")), str(result.get("message", ""))
            )
        return result

    def parse(
        self, path: Path, *, title: str, page_count: int, source_format: str = "pdf"
    ) -> ParsedDocument:
        if source_format != "pdf":
            try:
                return self._parse_structured(path, title, source_format)
            except SandboxError as exc:
                raise _error_from_sandbox(exc) from exc
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

    def _parse_structured(self, path: Path, title: str, source_format: str) -> ParsedDocument:
        """TXT, Markdown, CSV and DOCX: structure comes from the format, never from OCR."""
        s = self._s
        result = self._run("structured", path, 1, source_format)
        if "pages" in result:  # plain text: same repair + heuristics as PDF text
            chunks = self._chunk_text_pages(result["pages"], title)
        else:
            blocks = [
                Block(kind=str(b["kind"]), text=str(b["text"]), level=int(b.get("level", 1)))
                for b in result["blocks"]
            ]
            raw = chunk_blocks(
                blocks,
                target_tokens=s.chunk_target_tokens,
                max_tokens=s.chunk_max_tokens,
                min_tokens=s.chunk_min_tokens,
                overlap_tokens=s.chunk_overlap_tokens,
                min_chars=min(s.min_chunk_chars, 8),  # short table rows are still real content
            )
            chunks = [replace(c, embed_text=contextual_text(c, title=title)) for c in raw]
        return ParsedDocument(chunks, 1, "not_needed", 0, "structured")

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


def parse_bytes(
    parser: PdfParser,
    data: bytes,
    *,
    title: str,
    page_count: int,
    source_format: str = "pdf",
) -> ParsedDocument:
    """Write the document to a private temp file, parse it, delete it."""
    with tempfile.TemporaryDirectory(prefix="doc-") as directory:
        path = Path(directory) / f"document.{source_format}"
        path.write_bytes(data)
        return parser.parse(path, title=title, page_count=page_count, source_format=source_format)
