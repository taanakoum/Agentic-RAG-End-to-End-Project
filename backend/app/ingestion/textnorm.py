"""Dependency-free text helpers shared by the parent process and the sandboxed child."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

_NBSP = chr(0xA0)  # built from the code point so the source has no ambiguous literal
_WS = re.compile(rf"[ \t\f\v{_NBSP}]+")
_BLANKLINES = re.compile(r"\n{3,}")


@dataclass(frozen=True, slots=True)
class PageText:
    page_number: int  # 1-based
    text: str
    from_ocr: bool = False


def nfkc(text: str) -> str:
    """Compatibility normalisation without touching layout: PDF maths often arrives as
    'mathematical italic' letters (U+1D400 block) that look like N but are not N, so neither
    search nor a person's question matches them; NFKC maps them (and ligatures, full-width
    forms) to their plain equivalents."""
    return unicodedata.normalize("NFKC", text).replace("\x00", "")


def normalize_text(raw: str) -> str:
    text = unicodedata.normalize("NFKC", raw).replace("\x00", "")
    text = "\n".join(_WS.sub(" ", line).strip() for line in text.splitlines())
    return _BLANKLINES.sub("\n\n", text).strip()


def meaningful_chars(text: str) -> int:
    return sum(1 for ch in text if ch.isalnum())
