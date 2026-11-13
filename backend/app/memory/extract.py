"""Turn conversation messages into memory candidates (controlled consolidation).

`MemoryExtractor` is a protocol so the extraction strategy can change without touching the
pipeline. The default `RuleExtractor` stores ONLY what the user explicitly states about
themselves ("remember that ...", "my name is ...", "I prefer ...", "call me ..."): no model call,
deterministic, cheap and conservative. `app.memory.llm_extract.LlmMemoryExtractor` is the optional
model-based alternative (MEMORY_LLM_EXTRACTION=true, ADR-037); it falls back to this one on any error.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from app.memory.sensitive import sensitive_reason

MAX_MEMORY_CHARS = 500


@dataclass(frozen=True, slots=True)
class Candidate:
    kind: str  # semantic | episodic | summary
    content: str
    importance: int = 3


@dataclass(frozen=True, slots=True)
class ConversationTurn:
    role: str
    content: str


class MemoryExtractor(Protocol):
    def extract(self, turns: Sequence[ConversationTurn]) -> list[Candidate]: ...


_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
_RULES: tuple[tuple[re.Pattern[str], int, str], ...] = (
    # (pattern over ONE sentence of a user message, importance, memory template)
    (re.compile(r"^(?:please\s+)?remember\s+(?:that\s+)?(.{4,400})$", re.I), 4, "{0}"),
    (
        re.compile(r"^(?:my name is|i am called|call me)\s+([A-Za-z][\w' -]{0,60})$", re.I),
        5,
        "The user's name is {0}.",
    ),
    (
        re.compile(r"^i (?:really )?(?:prefer|like|love|enjoy)\s+(.{3,200})$", re.I),
        3,
        "The user prefers {0}.",
    ),
    (
        re.compile(r"^i (?:work|am working) (?:at|for)\s+(.{2,100})$", re.I),
        4,
        "The user works at {0}.",
    ),
    (re.compile(r"^i(?:'m| am) (?:a|an)\s+(.{3,100})$", re.I), 3, "The user is a {0}."),
    (
        re.compile(r"^i (?:always|usually) (?:use|write in|work with)\s+(.{2,120})$", re.I),
        3,
        "The user usually uses {0}.",
    ),
    (
        re.compile(r"^i (?:don't|do not|dislike|hate)\s+(?:like\s+)?(.{3,160})$", re.I),
        3,
        "The user dislikes {0}.",
    ),
)


def _clean(value: str) -> str:
    return " ".join(value.split()).rstrip(".!?, ")


class RuleExtractor:
    """Explicit self-statements only. Questions and hypotheticals are ignored."""

    def extract(self, turns: Sequence[ConversationTurn]) -> list[Candidate]:
        found: list[Candidate] = []
        seen: set[str] = set()
        for turn in turns:
            if turn.role != "user":
                continue
            for sentence in _SENTENCE_END.split(turn.content.strip()):
                sentence = sentence.strip()
                if not sentence or sentence.endswith("?"):
                    continue
                sentence = sentence.rstrip(
                    ".! "
                )  # the rules match the sentence without its end mark
                for pattern, importance, template in _RULES:
                    match = pattern.match(sentence)
                    if match is None:
                        continue
                    body = _clean(match.group(1))
                    content = template.format(body)
                    if template == "{0}":
                        content = body[0].upper() + body[1:] + "."
                    if len(content) > MAX_MEMORY_CHARS or sensitive_reason(content):
                        break
                    key = content.lower()
                    if key not in seen:
                        seen.add(key)
                        found.append(Candidate("semantic", content, importance))
                    break
        return found
