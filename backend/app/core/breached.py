"""Offline check against passwords known from public breaches (no external service, no network).

Data: the union of two public top-100k password lists from SecLists (MIT licence), lower-cased and
deduplicated (backend/app/core/data/common_passwords.txt). Because users usually just decorate a
known password ("Password2026!"), a candidate is also reduced to its core (trailing digits and
symbols removed, common leet substitutions undone) and checked again.

Limits: this is a heuristic over ~135k very common passwords, not the multi-billion-entry corpora
of online services; it cannot know about passwords leaked from other sites unless they are common.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

_FILE = Path(__file__).parent / "data" / "common_passwords.txt"
_TRAILING = re.compile(r"[^a-z]+$")
_LEADING = re.compile(r"^[^a-z]+")
_LEET = str.maketrans(
    {"4": "a", "@": "a", "3": "e", "0": "o", "1": "i", "!": "i", "$": "s", "5": "s", "7": "t"}
)
_MIN_CORE = 6  # shorter cores ("pass", "abc") would cause false positives


@lru_cache(maxsize=1)
def _common() -> frozenset[str]:
    return frozenset(_FILE.read_text(encoding="utf-8").split())


def is_breached_password(password: str) -> bool:
    lowered = password.strip().lower()
    common = _common()
    if lowered in common:
        return True
    for candidate in {lowered, lowered.translate(_LEET)}:
        core = _LEADING.sub("", _TRAILING.sub("", candidate))
        if len(core) >= _MIN_CORE and core in common:
            return True
        if candidate in common:
            return True
    return False
