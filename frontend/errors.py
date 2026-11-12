"""How backend errors are shown to a person.

The backend's messages are already safe, but several situations deserve a different, more useful
wording (and a different kind of widget): bad credentials, an unsupported vs a corrupted file, size
limits, outages, rate limits, a conversation that is busy. Unknown codes fall back to the server's
own message, never to a stack trace or a raw body.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from frontend.api import ApiError

Kind = Literal["input", "credentials", "document", "limit", "outage", "conflict", "auth", "other"]


@dataclass(frozen=True)
class Presentation:
    kind: Kind
    message: str
    fields: dict[str, str] = field(default_factory=dict[str, str])  # field name -> message
    retryable: bool = False  # only where repeating the SAME request is safe
    request_id: str | None = None

    def with_reference(self) -> str:
        return f"{self.message} (reference: {self.request_id})" if self.request_id else self.message


_MESSAGES: dict[str, tuple[Kind, str, bool]] = {
    "INVALID_CREDENTIALS": ("credentials", "The email or password is not correct.", False),
    "AUTHENTICATION_FAILED": ("credentials", "The email or password is not correct.", False),
    "EMAIL_ALREADY_REGISTERED": (
        "input",
        "An account with this email already exists. Try signing in instead.",
        False,
    ),
    "INVALID_MFA_CODE": ("credentials", "That verification code is not correct.", False),
    "INVALID_MFA_TOKEN": ("auth", "The sign-in took too long. Please start again.", False),
    "SESSION_REVOKED": ("auth", "This session was ended. Please sign in again.", False),
    "INVALID_TOKEN": ("auth", "Your session has expired. Please sign in again.", False),
    "INVALID_REFRESH_TOKEN": ("auth", "Your session has expired. Please sign in again.", False),
    "AUTHENTICATION_REQUIRED": ("auth", "Please sign in to continue.", False),
    "EMAIL_NOT_VERIFIED": ("auth", "Please verify your email address first.", False),
    "FORBIDDEN": ("auth", "You do not have permission to do that.", False),
    "RESOURCE_NOT_FOUND": ("other", "That item no longer exists.", False),
    "DOCUMENT_INVALID_FORMAT": (
        "document",
        "This file is not a supported document type, or its contents do not match its name.",
        False,
    ),
    "UNSUPPORTED_MEDIA_TYPE": (
        "document",
        "This file type is not supported. Upload a PDF, DOCX, TXT, MD or CSV file.",
        False,
    ),
    "DOCUMENT_UNSUPPORTED_TYPE": (
        "document",
        "This file type is not supported. Upload a PDF, DOCX, TXT, MD or CSV file.",
        False,
    ),
    "DOCUMENT_CORRUPTED": (
        "document",
        "The file looks damaged and could not be read. Try exporting it again.",
        False,
    ),
    "DOCUMENT_EMPTY": ("document", "The file is empty.", False),
    "DOCUMENT_ENCRYPTED": (
        "document",
        "The file is password-protected. Remove the password and upload it again.",
        False,
    ),
    "DOCUMENT_TOO_MANY_PAGES": ("document", "The document has too many pages.", False),
    "DOCUMENT_TOO_LARGE": ("limit", "The file is larger than the allowed size.", False),
    "PAYLOAD_TOO_LARGE": ("limit", "That is larger than the allowed size.", False),
    "DOCUMENT_NO_TEXT": (
        "document",
        "No readable text was found in this file (scanned pages may need OCR).",
        False,
    ),
    "RATE_LIMIT_EXCEEDED": ("limit", "Too many requests. Please wait a moment.", True),
    "MODEL_RATE_LIMITED": (
        "limit",
        "The language model is busy or its quota is used up for now. Please try again later.",
        True,
    ),
    "MODEL_TIMEOUT": ("outage", "The language model took too long to answer.", True),
    "MODEL_PROVIDER_ERROR": ("outage", "The language model is unavailable right now.", True),
    "MODEL_NOT_CONFIGURED": (
        "outage",
        "No language model is configured on the server, so questions cannot be answered.",
        False,
    ),
    "SERVICE_UNAVAILABLE": (
        "outage",
        "A required service is unavailable. Try again shortly.",
        True,
    ),
    "UPSTREAM_TIMEOUT": ("outage", "A service took too long to answer. Try again shortly.", True),
    "RETRIEVAL_TIMEOUT": ("outage", "Searching your documents took too long.", True),
    "NETWORK_ERROR": ("outage", "The server could not be reached. Check the connection.", True),
    "NETWORK_TIMEOUT": ("outage", "The server took too long to answer.", True),
    "STREAM_INTERRUPTED": (
        "outage",
        "The connection was interrupted before the answer arrived.",
        True,
    ),
    "CONCURRENCY_CONFLICT": (
        "conflict",
        "This conversation is still answering your previous question. Wait for it to finish.",
        True,
    ),
    "IDEMPOTENCY_KEY_REUSED": (
        "conflict",
        "That message was already sent. Reload the conversation to see the result.",
        False,
    ),
    "TURN_FAILED": ("outage", "The previous attempt failed. Please send the message again.", False),
    "PERSISTENCE_FAILED": (
        "outage",
        "The answer could not be saved, so it was not shown. Please try again.",
        True,
    ),
    "AGENT_EXECUTION_FAILED": ("outage", "Something went wrong while answering.", True),
    "UNEXPECTED_RESPONSE": ("outage", "The server sent an unexpected response.", True),
}


def describe(error: ApiError) -> Presentation:
    """Turn an ApiError into wording, field-level messages and a retry hint."""
    fields: dict[str, str] = {}
    if error.code == "VALIDATION_ERROR":
        for detail in error.details:
            name = str(detail.get("field") or "").split(".")[-1]
            if name and name not in fields:
                fields[name] = str(detail.get("message") or "Invalid value.")
        return Presentation(
            "input",
            "Please correct the highlighted fields." if fields else "Please check your input.",
            fields,
            request_id=error.request_id,
        )
    if error.code in _MESSAGES:
        kind, message, retryable = _MESSAGES[error.code]
        if error.code in {"RATE_LIMIT_EXCEEDED", "MODEL_RATE_LIMITED"} and error.retry_after_s:
            message = f"{message} Try again in about {error.retry_after_s} seconds."
        return Presentation(kind, message, retryable=retryable, request_id=error.request_id)
    kind_by_status: Kind = "outage" if error.status >= 500 else "other"
    return Presentation(
        kind_by_status,
        error.message or "Something went wrong.",
        retryable=error.status in {502, 503, 504},
        request_id=error.request_id,
    )


def stream_error(
    code: str, message: str, retryable: bool, retry_after_s: int | None
) -> Presentation:
    """The same wording for an `error` event that arrives inside a stream."""
    return describe(
        ApiError(
            status=503 if retryable else 500,
            code=code,
            message=message,
            retry_after_s=retry_after_s,
        )
    )
