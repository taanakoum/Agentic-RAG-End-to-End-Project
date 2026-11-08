"""Heuristic filter that keeps obviously sensitive data out of long-term memory.

Memory is for preferences and stable facts, so by default nothing that looks like a credential,
payment card, government id or contact detail is stored. This is a heuristic safety net (it cannot
understand context); the user can always view and delete memories.
"""

from __future__ import annotations

import re

_CARD_DIGITS = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")


def _luhn(digits: str) -> bool:
    total = 0
    for index, ch in enumerate(reversed(digits)):
        n = int(ch)
        if index % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def _payment_card(text: str) -> bool:
    for match in _CARD_DIGITS.finditer(text):
        digits = re.sub(r"\D", "", match.group())
        if 13 <= len(digits) <= 19 and _luhn(digits):
            return True
    return False


# Checked in this order, so the most specific reason wins (an IBAN also contains a long number).
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "credential",
        re.compile(r"\b(pass(word|phrase|code)?|pwd|secret|token|api[ _-]?key)\b", re.I),
    ),
    ("api_key", re.compile(r"\b(sk|pk|rk|ghp|gho|xox[bp])[-_][A-Za-z0-9_-]{16,}\b")),
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("url_credentials", re.compile(r"://[^/\s:@]+:[^/\s@]+@")),
    ("iban", re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b")),
    ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
)
_CONTACT: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("email", re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")),
    # 10-15 digits, optionally grouped with spaces, dots, dashes or parentheses
    ("phone", re.compile(r"(?<!\d)(?:\+?\d[\s().-]{0,2}){9,14}\d(?!\d)")),
)


def sensitive_reason(text: str) -> str | None:
    """Return a short reason code if the text looks sensitive, else None.

    Deliberately conservative: long digit groups are treated as contact/payment data even when
    they fail the card checksum, because losing a memory is cheaper than keeping a secret.
    """
    for reason, pattern in _PATTERNS:
        if pattern.search(text):
            return reason
    if _payment_card(text):
        return "payment_card"
    for reason, pattern in _CONTACT:
        if pattern.search(text):
            return reason
    return None
