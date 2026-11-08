"""Structure-aware recursive chunking. Chunks never cross page boundaries (exact page citations)."""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.ingestion.extraction import PageText, meaningful_chars

_SEPARATORS = ("\n\n", "\n", ". ", "? ", "! ", "; ", ", ", " ")
_NUMBERED = re.compile(r"^(\d+(\.\d+)*|[IVXLC]+)[.)]?\s+\S")
_ENDS_LIKE_SENTENCE = re.compile(r"[.,;:!?]$")


@dataclass(frozen=True, slots=True)
class Chunk:
    index: int
    page_number: int
    heading: str | None
    text: str


def looks_like_heading(line: str) -> bool:
    line = line.strip()
    if not 3 <= len(line) <= 80 or _ENDS_LIKE_SENTENCE.search(line):
        return False
    if line.isupper() or _NUMBERED.match(line):
        return True
    words = line.split()
    return len(words) <= 6 and all(w[:1].isupper() for w in words if w[:1].isalpha())


def _split_recursive(text: str, size: int, separators: tuple[str, ...]) -> list[str]:
    """Split into pieces no longer than `size`, preferring the coarsest separator that works."""
    if len(text) <= size:
        return [text]
    for sep in separators:
        if sep in text:
            parts = [p for p in text.split(sep) if p.strip()]
            rest = separators[separators.index(sep) + 1 :]
            pieces: list[str] = []
            for i, part in enumerate(parts):
                suffix = sep if i < len(parts) - 1 and sep.strip() else ""
                piece = part + suffix
                pieces.extend(_split_recursive(piece, size, rest) if len(piece) > size else [piece])
            return pieces
    return [text[i : i + size] for i in range(0, len(text), size)]  # no separator: hard split


def _merge(pieces: list[str], size: int, overlap: int) -> list[str]:
    """Greedily merge small pieces up to `size`, carrying a tail of <= `overlap` characters."""
    chunks: list[str] = []
    current: list[str] = []
    length = 0
    for piece in pieces:
        add = len(piece) + (1 if current else 0)
        if current and length + add > size:
            chunks.append(" ".join(current).strip())
            carried: list[str] = []
            carried_len = 0
            for prev in reversed(current):
                if carried_len + len(prev) + 1 > overlap:
                    break
                carried.insert(0, prev)
                carried_len += len(prev) + 1
            current, length = carried, carried_len
            add = len(piece) + (1 if current else 0)
        current.append(piece)
        length += add
    if current:
        chunks.append(" ".join(current).strip())
    return chunks


def chunk_pages(pages: list[PageText], *, size: int, overlap: int, min_chars: int) -> list[Chunk]:
    """Chunk each page; keep the running section heading; drop empty or meaningless chunks."""
    chunks: list[Chunk] = []
    heading: str | None = None
    for page in pages:
        if not page.text:
            continue
        for line in page.text.splitlines():
            if looks_like_heading(line):
                heading = line.strip()
        pieces = _split_recursive(page.text, size, _SEPARATORS)
        for text in _merge(pieces, size, overlap):
            alnum = meaningful_chars(text)
            if alnum < min_chars or alnum / max(len(text), 1) < 0.3:
                continue  # whitespace, page numbers, separator lines, OCR garbage
            chunks.append(
                Chunk(index=len(chunks), page_number=page.page_number, heading=heading, text=text)
            )
    return chunks
