"""Structure- and sentence-aware chunking with section context and cross-page continuity.

Design (see ADR-015):
  * Text is split into sections (headings) and sentences, never in the middle of a sentence
    unless one sentence alone exceeds the maximum chunk size.
  * Chunks are packed up to a token budget (target 256, hard max 384 by default; one token is
    estimated as 1.3 words), so they fit comfortably in a 512-token embedding model.
  * A chunk may span a page break (sentences continue across pages); `page_number` is the page
    where the chunk starts and `page_end` where it ends, so citations stay exact.
  * Each chunk records its section path ("Report > Financial summary"). The embedded text gets
    that path (and the document title) as a context header, which is what makes a chunk such as
    "Costs decreased 4%." retrievable by a question about the financial summary.
  * A small sentence-level overlap is carried to the next chunk inside the same section only.
  * Short sections are merged forward instead of becoming tiny fragments.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.ingestion.textnorm import PageText, meaningful_chars

_NUMBERED = re.compile(r"^(\d+(?:\.\d+)*|[IVXLC]+)[.)]?\s+\S")
_ENDS_LIKE_SENTENCE = re.compile(r"[.,;:!?]$")
_BULLET = re.compile(r"^\s*(?:[-*•●▪]|\d{1,3}[.)]|[a-zA-Z][.)])\s+\S")
_SENTENCE_BREAK = re.compile(r"([.!?]+[\"')\]]*)\s+(?=[\"'(\[]?[A-Z0-9])")
_ABBREVIATIONS = frozenset(
    {
        "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "vs", "etc", "e.g", "i.e", "fig", "no",
        "inc", "ltd", "co", "st", "approx", "dept", "al", "cf", "eq", "sec", "vol", "pp",
    }
)  # fmt: skip
_TOKENS_PER_WORD = 1.3


@dataclass(frozen=True, slots=True)
class Chunk:
    index: int
    page_number: int  # page on which the chunk starts (used for citations)
    page_end: int
    heading: str | None  # deepest heading in force at the start of the chunk
    section_path: str  # "Parent > Child" or ""
    text: str
    tokens: int
    embed_text: str = ""  # text actually embedded (context header + text); set by the parser


@dataclass(frozen=True, slots=True)
class _Unit:
    text: str
    page: int
    kind: str  # "sentence" | "item" | "heading"


def estimate_tokens(text: str) -> int:
    return max(1, round(len(text.split()) * _TOKENS_PER_WORD))


def looks_like_heading(line: str) -> bool:
    line = line.strip()
    if not 3 <= len(line) <= 80 or _ENDS_LIKE_SENTENCE.search(line):
        return False
    if line.isupper() or _NUMBERED.match(line):
        return True
    words = line.split()
    return len(words) <= 6 and all(w[:1].isupper() for w in words if w[:1].isalpha())


def heading_level(line: str) -> int:
    line = line.strip()
    numbered = _NUMBERED.match(line)
    if numbered and numbered.group(1)[0].isdigit():
        return numbered.group(1).count(".") + 1
    return 1 if line.isupper() else 2


def split_sentences(text: str) -> list[str]:
    """Rule-based sentence splitter that respects common abbreviations and initials."""
    sentences: list[str] = []
    start = 0
    for match in _SENTENCE_BREAK.finditer(text):
        before = text[start : match.start(1)]
        last_word = before.split()[-1].lower().rstrip(".") if before.split() else ""
        initial = len(last_word) == 1 and last_word.isalpha()
        if last_word in _ABBREVIATIONS or initial:
            continue
        sentence = text[start : match.end(1)].strip()
        if sentence:
            sentences.append(sentence)
        start = match.end()
    tail = text[start:].strip()
    if tail:
        sentences.append(tail)
    return sentences


def _split_oversized(text: str, max_tokens: int) -> list[str]:
    """Break a single over-long sentence on clause separators, then on words."""
    if estimate_tokens(text) <= max_tokens:
        return [text]
    for separator in ("; ", ", ", " - ", " "):
        pieces = [p for p in text.split(separator) if p.strip()]
        if len(pieces) > 1:
            out: list[str] = []
            current = ""
            for piece in pieces:
                candidate = f"{current}{separator}{piece}" if current else piece
                if current and estimate_tokens(candidate) > max_tokens:
                    out.append(current)
                    current = piece
                else:
                    current = candidate
            if current:
                out.append(current)
            if all(estimate_tokens(p) <= max_tokens for p in out):
                return out
            return [q for p in out for q in _split_oversized(p, max_tokens)]
    step = max(8, int(max_tokens / _TOKENS_PER_WORD) * 6)  # one unbroken blob: cut by characters
    return [text[i : i + step] for i in range(0, len(text), step)]


def _paragraph_units(lines: list[str], page: int, max_tokens: int) -> list[tuple[_Unit, int]]:
    out: list[tuple[_Unit, int]] = []
    if not lines:
        return out
    if all(_BULLET.match(ln) for ln in lines) and len(lines) > 1:
        for line in lines:  # a list: keep each item whole
            out.extend((_Unit(p, page, "item"), 0) for p in _split_oversized(line, max_tokens))
        return out
    for sentence in split_sentences(" ".join(lines)):
        out.extend((_Unit(p, page, "sentence"), 0) for p in _split_oversized(sentence, max_tokens))
    return out


def _units(pages: list[PageText], max_tokens: int) -> list[tuple[_Unit, int]]:
    """Flatten pages into (unit, heading_level) in reading order; level 0 = not a heading.

    Headings are recognised line by line, because extracted PDF text usually has no blank line
    after a heading: a short line without sentence punctuation that starts a block or follows a
    line that ended a sentence is a heading.
    """
    out: list[tuple[_Unit, int]] = []
    for page in pages:
        for block in re.split(r"\n{2,}", page.text):
            buffer: list[str] = []
            for line in (ln.strip() for ln in block.splitlines()):
                if not line:
                    continue
                at_boundary = not buffer or buffer[-1].rstrip().endswith((".", "!", "?", ":"))
                if at_boundary and looks_like_heading(line) and not _BULLET.match(line):
                    out.extend(_paragraph_units(buffer, page.page_number, max_tokens))
                    buffer = []
                    out.append((_Unit(line, page.page_number, "heading"), heading_level(line)))
                else:
                    buffer.append(line)
            out.extend(_paragraph_units(buffer, page.page_number, max_tokens))
    return out


def chunk_pages(
    pages: list[PageText],
    *,
    target_tokens: int = 256,
    max_tokens: int = 384,
    min_tokens: int = 48,
    overlap_tokens: int = 40,
    min_chars: int = 30,
) -> list[Chunk]:
    """Pack sentences into section-aware chunks. See the module docstring for the rules."""
    chunks: list[Chunk] = []
    stack: list[tuple[int, str]] = []
    current: list[_Unit] = []
    tokens = 0
    start_path = ""
    start_heading: str | None = None

    def path_now() -> tuple[str, str | None]:
        return " > ".join(t for _, t in stack), (stack[-1][1] if stack else None)

    def flush(*, carry_overlap: bool) -> None:
        nonlocal current, tokens, start_path, start_heading
        if not current:
            return
        text = _join(current)
        alnum = meaningful_chars(text)
        if alnum >= min_chars and alnum / max(len(text), 1) >= 0.3:
            chunks.append(
                Chunk(
                    index=len(chunks),
                    page_number=current[0].page,
                    page_end=current[-1].page,
                    heading=start_heading,
                    section_path=start_path,
                    text=text,
                    tokens=estimate_tokens(text),
                )
            )
        carried: list[_Unit] = []
        if carry_overlap:
            budget = 0
            for unit in reversed(current):
                cost = estimate_tokens(unit.text)
                if unit.kind == "heading" or budget + cost > overlap_tokens:
                    break
                carried.insert(0, unit)
                budget += cost
            if len(carried) == len(current):  # never carry everything: guarantees progress
                carried = carried[1:]
        current = carried
        tokens = sum(estimate_tokens(u.text) for u in carried)
        start_path, start_heading = path_now()

    for unit, level in _units(pages, max_tokens):
        cost = estimate_tokens(unit.text)
        if unit.kind == "heading":
            if tokens >= min_tokens:
                flush(carry_overlap=False)  # a section boundary: start fresh, no overlap
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, unit.text))
            if not current:
                start_path, start_heading = path_now()
            current.append(unit)
            tokens += cost
            continue
        if current and tokens + cost > target_tokens and tokens >= min_tokens:
            flush(carry_overlap=True)
        if not current:
            start_path, start_heading = path_now()
        current.append(unit)
        tokens += cost
    flush(carry_overlap=False)
    return chunks


def _join(units: list[_Unit]) -> str:
    parts: list[str] = []
    for i, unit in enumerate(units):
        if i == 0:
            parts.append(unit.text)
        elif unit.kind in {"item", "heading"} or units[i - 1].kind in {"item", "heading"}:
            parts.append("\n" + unit.text)
        else:
            parts.append(" " + unit.text)
    return "".join(parts)


def contextual_text(chunk: Chunk, *, title: str) -> str:
    """What gets embedded: document title and section path give the chunk its context."""
    header = f"{title} > {chunk.section_path}" if chunk.section_path else title
    return f"{header}\n{chunk.text}"
