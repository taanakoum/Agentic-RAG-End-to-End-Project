"""Turn raw model text into a validated Pydantic object, or a short list of safe error hints.

Free hosted models are inconsistent about JSON: they wrap it in markdown fences, add a sentence
before or after it, or use a stray trailing comma. This parser tolerates the first two (fences,
surrounding prose) and rejects everything else; it NEVER repairs by guessing. Error hints contain
only field paths and error kinds, never values, so they can be sent back to the model for a
bounded repair attempt without echoing model- or user-supplied text.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

MAX_JSON_CHARS = 100_000


@dataclass(frozen=True, slots=True)
class ParseResult[M: BaseModel]:
    value: M | None
    errors: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.value is not None


def extract_json_object(text: str) -> str | None:
    """The first balanced {...} object in `text` (string- and escape-aware), or None."""
    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            ch = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start : index + 1]
        start = text.find("{", start + 1)
    return None


def _hint(error: dict[str, Any]) -> str:
    path = ".".join(str(part) for part in error.get("loc", ())) or "(root)"
    return f"{path}: {error.get('type', 'invalid')}"


def parse_model_json[M: BaseModel](text: str, model: type[M]) -> ParseResult[M]:
    if len(text) > MAX_JSON_CHARS:
        return ParseResult(None, ("(root): too_long",))
    candidate = extract_json_object(text)
    if candidate is None:
        return ParseResult(None, ("(root): no_json_object",))
    try:
        data = json.loads(candidate)
    except ValueError:
        return ParseResult(None, ("(root): malformed_json",))
    try:
        return ParseResult(model.model_validate(data))
    except ValidationError as exc:
        return ParseResult(None, tuple(_hint(dict(e)) for e in exc.errors()[:8]))
