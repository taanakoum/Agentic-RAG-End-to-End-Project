"""Chat endpoints: one JSON answer, or the same turn as a server-sent event stream."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header
from fastapi.responses import StreamingResponse

from app.core.exceptions import ErrorDetail, RequestValidationAppError, error_responses
from app.dependencies.auth import ChatServiceDep, VerifiedUser
from app.schemas.chat_stream import ChatRequest, ChatResult

router = APIRouter(prefix="/chat", tags=["chat"])

IdempotencyKey = Annotated[
    str | None,
    Header(
        alias="Idempotency-Key",
        description="Client-chosen key (8-128 chars of A-Z a-z 0-9 _ . : -), unique per session. "
        "Retrying with the same key never creates a second answer.",
    ),
]
_ERRORS = error_responses(401, 403, 404, 409, 422, 429, 500, 502, 503, 504)


def _key(value: str | None) -> str:
    if not value:
        raise RequestValidationAppError(
            details=[
                ErrorDetail(
                    field="Idempotency-Key",
                    code="missing",
                    message="Send an Idempotency-Key header.",
                )
            ]
        )
    return value


@router.post("", response_model=ChatResult, responses=_ERRORS)
async def chat(
    body: ChatRequest,
    current: VerifiedUser,
    service: ChatServiceDep,
    idempotency_key: IdempotencyKey = None,
) -> ChatResult:
    """Ask a question about your documents and get one validated, cited answer."""
    return await service.answer(
        current, body.session_id, idempotency_key=_key(idempotency_key), message=body.message
    )


@router.post(
    "/stream",
    responses={
        200: {
            "content": {"text/event-stream": {}},
            "description": "SSE: start, status*, token*, citation*, complete | error",
        },
        **_ERRORS,
    },
)
async def chat_stream(
    body: ChatRequest,
    current: VerifiedUser,
    service: ChatServiceDep,
    idempotency_key: IdempotencyKey = None,
) -> StreamingResponse:
    """The same turn as server-sent events. Refusals happen before the stream opens (JSON)."""
    planned = await service.prepare(
        current, body.session_id, idempotency_key=_key(idempotency_key), message=body.message
    )
    return StreamingResponse(
        service.stream(planned, body.session_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
