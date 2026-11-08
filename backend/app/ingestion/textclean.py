"""Repair PDF text before chunking: running headers/footers, page numbers, hyphenation, wrapping.

PDF text extraction returns visual lines, not sentences. Chunking those directly splits sentences
at line breaks, embeds page furniture ("Page 3 of 10", the report title on every page) and
breaks words at line ends ("informa-\\ntion"). Cleaning first keeps the information and makes
the embedded text match what a user would query.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import replace

from app.ingestion.textnorm import PageText

_PAGE_NUMBER = re.compile(r"^\W*(?:page\s+)?\d{1,4}(?:\s*(?:/|of)\s*\d{1,4})?\W*$", re.IGNORECASE)
_DIGITS = re.compile(r"\d+")
_HYPHEN_BREAK = re.compile(r"(?<=[A-Za-z]{2})-\n(?=[a-z]{2})")
_BULLET = re.compile(r"^\s*(?:[-*•●▪]|\d{1,3}[.)]|[a-zA-Z][.)])\s+\S")
_SENTENCE_END = re.compile(r"[.!?:;][\"')\]]?$")


def _norm_line(line: str) -> str:
    return _DIGITS.sub("#", line.strip().lower())


def _edge_lines(text: str) -> list[str]:
    """First and last lines of a page: one per side on short pages, two on full-length pages."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    count = 2 if len(lines) >= 12 else 1
    return lines[:count] + lines[-count:]


def find_page_furniture(pages: list[PageText]) -> set[str]:
    """Normalised lines that repeat on many pages' first/last lines (headers, footers, titles)."""
    if len(pages) < 3:
        return set()
    seen: Counter[str] = Counter()
    for page in pages:
        for line in {_norm_line(ln) for ln in _edge_lines(page.text)}:
            if 3 <= len(line) <= 120:
                seen[line] += 1
    threshold = max(3, int(len(pages) * 0.4))
    return {line for line, n in seen.items() if n >= threshold}


def _should_join(prev: str, nxt: str) -> bool:
    """True if `nxt` continues the sentence/paragraph that `prev` started."""
    if not prev or not nxt:
        return False
    if _BULLET.match(nxt) or (_BULLET.match(prev) and len(prev) < 60):
        return False
    if _SENTENCE_END.search(prev):
        return False
    return nxt[0].islower() or len(prev) >= 45


def _unwrap(text: str) -> str:
    """Join wrapped lines into paragraphs; keep blank-line paragraph breaks and list items."""
    out: list[str] = []
    for paragraph in re.split(r"\n{2,}", text):
        lines = [ln.strip() for ln in paragraph.splitlines() if ln.strip()]
        merged: list[str] = []
        for line in lines:
            if merged and _should_join(merged[-1], line):
                merged[-1] = f"{merged[-1]} {line}"
            else:
                merged.append(line)
        if merged:
            out.append("\n".join(merged))
    return "\n\n".join(out)


def clean_pages(pages: list[PageText]) -> list[PageText]:
    furniture = find_page_furniture(pages)
    cleaned: list[PageText] = []
    for page in pages:
        lines: list[str] = []
        for line in page.text.splitlines():
            if _PAGE_NUMBER.match(line) or _norm_line(line) in furniture:
                continue
            lines.append(line)
        text = _HYPHEN_BREAK.sub("", "\n".join(lines))
        cleaned.append(replace(page, text=_unwrap(text)))
    return cleaned
