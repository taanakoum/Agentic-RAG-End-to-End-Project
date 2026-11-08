"""Request/correlation ID propagation via contextvars."""

from __future__ import annotations

import re
import uuid
from contextvars import ContextVar

REQUEST_ID_HEADER = "X-Request-ID"

_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)
_VALID_ID = re.compile(r"^[A-Za-z0-9._-]{8,64}$")


def new_request_id() -> str:
    return uuid.uuid4().hex


def sanitize_request_id(candidate: str | None) -> str:
    """Accept a caller-supplied ID only if it is short and made of safe characters."""
    if candidate and _VALID_ID.fullmatch(candidate):
        return candidate
    return new_request_id()


def get_request_id() -> str | None:
    return _request_id.get()


def set_request_id(value: str | None) -> None:
    _request_id.set(value)
