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


def normalize_text(raw: str) -> str:
    text = unicodedata.normalize("NFKC", raw).replace("\x00", "")
    text = "\n".join(_WS.sub(" ", line).strip() for line in text.splitlines())
    return _BLANKLINES.sub("\n\n", text).strip()


def meaningful_chars(text: str) -> int:
    return sum(1 for ch in text if ch.isalnum())
