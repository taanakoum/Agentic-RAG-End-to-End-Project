"""Chat request/response contracts and the typed SSE events.

Event order of a streamed turn (docs/spec/05 "Streaming"):
    start, status*, token*, citation*, complete        (success)
    start, status*, error                              (failure after the stream opened)
Failures BEFORE the stream opens (unknown session, busy conversation, bad key, rate limit, no
model configured) are ordinary JSON error responses with HTTP status codes. `token` events carry
the already VALIDATED and already SAVED answer in slices: nothing unvalidated or unsaved is ever
streamed, and a replay of a finished turn produces the same events.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Iterator
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.agents.schemas import Citation, RAGResponse

Stage = Literal["retrieving", "generating", "repairing", "verifying", "saving"]


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: uuid.UUID
    message: str = Field(min_length=1, max_length=100000)


class ChatResult(BaseModel):
    session_id: uuid.UUID
    execution_id: uuid.UUID
    user_message_id: uuid.UUID
    assistant_message_id: uuid.UUID
    replayed: bool = False
    response: RAGResponse


class StartEvent(BaseModel):
    type: Literal["start"] = "start"
    session_id: uuid.UUID
    execution_id: uuid.UUID
    user_message_id: uuid.UUID
    replayed: bool = False


class StatusEvent(BaseModel):
    type: Literal["status"] = "status"
    stage: Stage


class TokenEvent(BaseModel):
    type: Literal["token"] = "token"
    index: int
    text: str


class CitationEvent(BaseModel):
    type: Literal["citation"] = "citation"
    index: int
    citation: Citation


class ErrorEvent(BaseModel):
    type: Literal["error"] = "error"
    code: str
    message: str
    retryable: bool = False
    retry_after_s: int | None = None


class CompleteEvent(BaseModel):
    type: Literal["complete"] = "complete"
    assistant_message_id: uuid.UUID
    response: RAGResponse


ChatEvent = Annotated[
    StartEvent | StatusEvent | TokenEvent | CitationEvent | ErrorEvent | CompleteEvent,
    Field(discriminator="type"),
]
AnyEvent = StartEvent | StatusEvent | TokenEvent | CitationEvent | ErrorEvent | CompleteEvent

KEEP_ALIVE = b": keep-alive\n\n"
_SLICE = re.compile(r"\S+\s*")


def sse(event: AnyEvent) -> bytes:
    """One server-sent event frame."""
    return f"event: {event.type}\ndata: {event.model_dump_json()}\n\n".encode()


def token_slices(answer: str, *, target: int = 32) -> Iterator[str]:
    """The answer in word-boundary slices of about `target` characters (concatenate = answer)."""
    buffer = ""
    consumed = 0
    for match in _SLICE.finditer(answer):
        buffer += match.group()
        consumed = match.end()
        if len(buffer) >= target:
            yield buffer
            buffer = ""
    tail = buffer + answer[consumed:]
    if tail:
        yield tail


def parse_sse(frame: bytes) -> dict[str, object]:
    """Test/client helper: decode one SSE frame into its JSON data (with the event name)."""
    text = frame.decode()
    name = next(line[7:] for line in text.splitlines() if line.startswith("event: "))
    data = next(line[6:] for line in text.splitlines() if line.startswith("data: "))
    payload: dict[str, object] = json.loads(data)
    assert payload["type"] == name  # noqa: S101 - frame integrity check for clients/tests
    return payload
