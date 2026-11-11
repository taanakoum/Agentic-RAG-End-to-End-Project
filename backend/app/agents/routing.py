"""Cheap, deterministic routing: no model call is spent on deciding what to do.

Routes: `smalltalk` (greeting, thanks, bye, what-can-you-do: answered from fixed text, no LLM),
`clarify` (nothing answerable in the message), `kb` (everything else: search the user's documents).
Asking the knowledge base first is the safe default: when nothing relevant exists the answer is an
honest "not found in your documents", never an invention. Smalltalk matching is a FULL match on an
allowlist, so "hi, my SSN is ..." or "hello, summarise the report" are never treated as smalltalk.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

Route = Literal["smalltalk", "clarify", "kb"]
SmalltalkKind = Literal["greeting", "thanks", "bye", "capabilities", "wellbeing"]

_STOPWORDS = frozenset(
    {
        # articles, auxiliaries, pronouns, politeness: carry no topic
        "a", "an", "the", "is", "are", "was", "were", "be", "been", "am", "do", "does", "did",
        "of", "to", "in", "on", "at", "for", "with", "and", "or", "but", "it", "this", "that",
        "these", "those", "i", "me", "my", "you", "your", "we", "us", "our", "they", "them",
        "he", "she", "his", "her", "its", "so", "if", "then", "than", "as", "by", "from",
        "please", "can", "could", "would", "should", "will", "just", "um", "uh", "ok", "okay",
        "yes", "no", "not",
        # question words and placeholders: "what is it?" names nothing to look up
        "what", "which", "who", "whom", "how", "when", "where", "why", "there", "here", "one",
        "ones", "thing", "things", "something", "anything", "stuff", "tell", "about", "much",
        "many",
    }
)  # fmt: skip
_SMALLTALK: tuple[tuple[SmalltalkKind, re.Pattern[str]], ...] = (
    (
        "greeting",
        re.compile(
            r"^(hi|hello|hey|hiya|howdy|yo|greetings|good (morning|afternoon|evening|day))"
            r"( there| all| everyone| team)?$"
        ),
    ),
    (
        "thanks",
        re.compile(r"^(thanks|thank you|thx|cheers|many thanks|ty)( (a lot|so much|very much))?$"),
    ),
    ("bye", re.compile(r"^(bye|goodbye|see you|see ya|good night|cya)( (later|soon|tomorrow))?$")),
    (
        "capabilities",
        re.compile(
            r"^(what can you do|what do you do|who are you|what are you|help|help me"
            r"|how do you work|how can you help( me)?|what can i ask( you)?)$"
        ),
    ),
    (
        "wellbeing",
        re.compile(r"^(how are you|how are you doing|how's it going|how is it going)$"),
    ),
)
_ANAPHORA = re.compile(
    r"\b(it|its|that|this|they|them|their|those|these|he|she|him|her"
    r"|the (first|second|third|last|previous|former|latter)"
    r" (one|point|part|section|document|file|item|option)"
    r"|what about|and the|how about|same|also|more|why)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class Routing:
    route: Route
    reason: str
    smalltalk: SmalltalkKind | None = None


def clean(message: str) -> str:
    """Lowercase, drop punctuation and emoji, collapse spaces."""
    text = re.sub(r"[^\w\s']", " ", message.lower())
    return " ".join(text.split())


def content_tokens(message: str) -> list[str]:
    return [t for t in clean(message).split() if t not in _STOPWORDS]


def classify(message: str) -> Routing:
    cleaned = clean(message)
    if not cleaned:
        return Routing("clarify", "empty_message")
    for kind, pattern in _SMALLTALK:
        if pattern.fullmatch(cleaned):
            return Routing("smalltalk", f"smalltalk_{kind}", kind)
    if not content_tokens(message):
        return Routing("clarify", "no_content_words")
    return Routing("kb", "default_kb")


def needs_rewrite(message: str, *, has_history: bool) -> bool:
    """One extra model call is only worth it for short follow-ups that lean on earlier turns."""
    if not has_history:
        return False
    tokens = clean(message).split()
    return len(tokens) <= 12 and bool(_ANAPHORA.search(message))
