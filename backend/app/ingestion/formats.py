"""Parsers for non-PDF documents: TXT, Markdown, CSV and DOCX (all run in the sandbox child).

Each parser returns plain dicts so the result can cross the process boundary as JSON:
  blocks: {"kind": "heading"|"paragraph"|"item"|"row", "text": str, "level": int}
Structure is taken from the format itself (Markdown `#`, DOCX heading styles, CSV header row),
so no heading guessing is needed, and every table row carries its column names so a row such as
"Region: North; Revenue: 4.2M" stays meaningful when retrieved on its own.
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from pathlib import Path
from typing import Any

SUPPORTED_FORMATS = ("pdf", "docx", "txt", "md", "csv")
Block = dict[str, Any]

_MD_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_MD_LIST = re.compile(r"^\s*(?:[-*+]|\d{1,3}[.)])\s+(.*)$")
_MD_FENCE = re.compile(r"^\s*(```|~~~)")
_MD_RULE = re.compile(r"^\s*(?:[-*_]\s*){3,}$")
_MD_SETEXT = re.compile(r"^\s*(=+|-+)\s*$")
_MD_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_MD_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_MD_EMPH = re.compile(r"(\*\*|__|\*|_|`|~~)")
_HTML_TAG = re.compile(r"<[^>]{1,200}>")


class FormatError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ------------------------------------------------------------------ text decoding


def read_text(path: str, *, max_bytes: int) -> str:
    """Decode a text file strictly (UTF-8 with optional BOM, or UTF-16 with BOM); reject binary."""
    raw = Path(path).read_bytes()
    if len(raw) > max_bytes:
        raise FormatError("DOCUMENT_TOO_LARGE", "The text file is larger than the allowed size.")
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        encoding = "utf-16"
    else:
        encoding = "utf-8-sig"
        if b"\x00" in raw[:8192]:
            raise FormatError("DOCUMENT_INVALID_FORMAT", "The file looks binary, not text.")
    try:
        text = raw.decode(encoding)
    except UnicodeDecodeError as exc:
        raise FormatError(
            "DOCUMENT_INVALID_ENCODING",
            "The text must be UTF-8 (or UTF-16 with a byte-order mark).",
        ) from exc
    if not text.strip():
        raise FormatError("DOCUMENT_EMPTY", "The file contains no text.")
    return text.replace("\r\n", "\n").replace("\r", "\n")


# ------------------------------------------------------------------ markdown


def _inline(text: str) -> str:
    text = _MD_LINK.sub(r"\1", text)
    text = _HTML_TAG.sub("", text)
    return _MD_EMPH.sub("", text).strip()


def parse_markdown(text: str) -> list[Block]:
    blocks: list[Block] = []
    paragraph: list[str] = []
    table: list[list[str]] = []
    in_fence = False
    fence_lines: list[str] = []

    def flush_paragraph() -> None:
        if paragraph:
            blocks.append({"kind": "paragraph", "text": _inline(" ".join(paragraph)), "level": 0})
            paragraph.clear()

    def flush_table() -> None:
        if table:
            header, *rows = table
            for row in rows:
                cells = [f"{h}: {c}" for h, c in zip(header, row, strict=False) if c]
                if cells:
                    blocks.append({"kind": "row", "text": "; ".join(cells), "level": 0})
            table.clear()

    lines = text.split("\n")
    for index, line in enumerate(lines):
        if _MD_FENCE.match(line):
            if in_fence:  # closing fence: keep the code as one block
                if fence_lines:
                    blocks.append({"kind": "item", "text": "\n".join(fence_lines), "level": 0})
                fence_lines = []
            else:
                flush_paragraph()
                flush_table()
            in_fence = not in_fence
            continue
        if in_fence:
            fence_lines.append(line.rstrip())
            continue
        stripped = line.strip()
        if not stripped or (_MD_RULE.match(line) and not paragraph):
            flush_paragraph()
            flush_table()
            continue
        heading = _MD_HEADING.match(line)
        if heading:
            flush_paragraph()
            flush_table()
            blocks.append(
                {
                    "kind": "heading",
                    "text": _inline(heading.group(2)),
                    "level": len(heading.group(1)),
                }
            )
            continue
        nxt = lines[index + 1] if index + 1 < len(lines) else ""
        if paragraph == [] and _MD_SETEXT.match(nxt) and not _MD_LIST.match(line) and stripped:
            flush_table()
            blocks.append(
                {
                    "kind": "heading",
                    "text": _inline(stripped),
                    "level": 1 if nxt.strip()[0] == "=" else 2,
                }
            )
            lines[index + 1] = ""  # consume the underline
            continue
        if stripped.startswith("|"):
            flush_paragraph()
            if _MD_TABLE_SEP.match(line):
                continue
            table.append([_inline(c) for c in stripped.strip("|").split("|")])
            continue
        flush_table()
        item = _MD_LIST.match(line)
        if item:
            flush_paragraph()
            blocks.append({"kind": "item", "text": _inline(item.group(1)), "level": 0})
            continue
        paragraph.append(stripped.lstrip("> "))
    flush_paragraph()
    flush_table()
    if in_fence and fence_lines:
        blocks.append({"kind": "item", "text": "\n".join(fence_lines), "level": 0})
    return [b for b in blocks if b["text"]]


# ------------------------------------------------------------------ csv


def parse_csv(text: str, *, max_rows: int, max_cols: int) -> list[Block]:
    sample = text[:65536]
    try:
        dialect: Any = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    csv.field_size_limit(1_000_000)
    try:
        rows = [r for r in csv.reader(io.StringIO(text), dialect) if any(c.strip() for c in r)]
    except csv.Error as exc:
        raise FormatError("DOCUMENT_CORRUPTED", f"The CSV could not be parsed: {exc}") from exc
    if not rows:
        raise FormatError("DOCUMENT_EMPTY", "The CSV contains no rows.")
    if len(rows) - 1 > max_rows:
        raise FormatError("DOCUMENT_TOO_MANY_ROWS", f"The CSV has more than {max_rows} data rows.")
    width = max(len(r) for r in rows)
    if width > max_cols:
        raise FormatError("DOCUMENT_TOO_MANY_COLUMNS", f"The CSV has more than {max_cols} columns.")
    first = [c.strip() for c in rows[0]]
    looks_like_header = len(rows) > 1 and all(c and not _is_number(c) for c in first)
    header = first if looks_like_header else [f"column {i + 1}" for i in range(width)]
    data = rows[1:] if looks_like_header else rows
    header += [f"column {i + 1}" for i in range(len(header), width)]
    blocks: list[Block] = [
        {"kind": "paragraph", "text": f"Columns: {', '.join(h for h in header if h)}.", "level": 0}
    ]
    for row in data:
        cells = [f"{h}: {c.strip()}" for h, c in zip(header, row, strict=False) if c.strip()]
        if cells:
            blocks.append({"kind": "row", "text": "; ".join(cells), "level": 0})
    return blocks


def _is_number(value: str) -> bool:
    try:
        float(value.replace(",", ""))
    except ValueError:
        return False
    return True


# ------------------------------------------------------------------ docx


def check_docx_zip(path: str, *, max_entries: int, max_uncompressed: int, max_ratio: int) -> None:
    """Reject zip bombs and macro-enabled packages before anything is parsed."""
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            names = {i.filename for i in infos}
            if "word/document.xml" not in names or "[Content_Types].xml" not in names:
                raise FormatError(
                    "DOCUMENT_INVALID_FORMAT", "The file is not a Word (.docx) document."
                )
            if any(n.lower().endswith("vbaproject.bin") for n in names):
                raise FormatError("DOCUMENT_UNSAFE", "Documents with macros are not accepted.")
            if len(infos) > max_entries:
                raise FormatError("DOCUMENT_UNSAFE", "The document contains too many parts.")
            total = 0
            for info in infos:
                total += info.file_size
                if (
                    info.file_size > 1024
                    and info.file_size / max(info.compress_size, 1) > max_ratio
                ):
                    raise FormatError("DOCUMENT_UNSAFE", "The document is suspiciously compressed.")
            if total > max_uncompressed:
                raise FormatError("DOCUMENT_UNSAFE", "The document expands to an unsafe size.")
    except zipfile.BadZipFile as exc:
        raise FormatError(
            "DOCUMENT_INVALID_FORMAT", "The file is not a valid .docx package."
        ) from exc


def parse_docx(path: str, *, max_rows: int) -> list[Block]:
    from docx import Document  # python-docx (MIT); imported lazily, child process only

    document: Any = Document(path)
    body: Any = document.element.body
    blocks: list[Block] = []
    paragraph_by_element = {p._element: p for p in document.paragraphs}
    table_by_element = {t._element: t for t in document.tables}
    rows_seen = 0
    for child in body.iterchildren():
        paragraph: Any = paragraph_by_element.get(child)
        if paragraph is not None:
            text = str(paragraph.text).strip()
            if not text:
                continue
            style = str(getattr(paragraph.style, "name", "") or "")
            lowered = style.lower()
            if lowered == "title":
                blocks.append({"kind": "heading", "text": text, "level": 1})
            elif lowered.startswith("heading"):
                digits = "".join(ch for ch in style if ch.isdigit())
                level = int(digits) if digits else 1
                blocks.append({"kind": "heading", "text": text, "level": min(level, 6)})
            elif "list" in lowered or (
                paragraph._p.pPr is not None and paragraph._p.pPr.numPr is not None
            ):
                blocks.append({"kind": "item", "text": text, "level": 0})
            else:
                blocks.append({"kind": "paragraph", "text": text, "level": 0})
            continue
        table: Any = table_by_element.get(child)
        if table is not None:
            grid = [[str(c.text).strip() for c in row.cells] for row in table.rows]
            if not grid:
                continue
            header, *rows = grid
            for row in rows:
                rows_seen += 1
                if rows_seen > max_rows:
                    raise FormatError(
                        "DOCUMENT_TOO_MANY_ROWS", "The document has too many table rows."
                    )
                cells = [f"{h}: {c}" for h, c in zip(header, row, strict=False) if c]
                if cells:
                    blocks.append({"kind": "row", "text": "; ".join(cells), "level": 0})
    if not blocks:
        raise FormatError("DOCUMENT_EMPTY", "The document contains no text.")
    return blocks
