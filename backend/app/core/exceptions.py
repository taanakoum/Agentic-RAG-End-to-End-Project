"""Central exception hierarchy, error contract and handlers.

Errors are raised from services as AppError subclasses and rendered here, once. Responses never
contain stack traces or input values (a failed password field must not echo the password).
"""

from __future__ import annotations

from typing import Any, cast

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import get_logger
from app.core.request_context import REQUEST_ID_HEADER, get_request_id

log = get_logger(__name__)


class ErrorDetail(BaseModel):
    field: str | None = None
    code: str
    message: str


class ErrorBody(BaseModel):
    code: str
    message: str
    details: list[ErrorDetail]
    request_id: str | None


class ErrorResponse(BaseModel):
    error: ErrorBody


class AppError(Exception):
    """Base for all expected, user-presentable errors."""

    status_code: int = 500
    code: str = "INTERNAL_ERROR"
    message: str = "An internal error occurred."

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        details: list[ErrorDetail] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message or self.message)
        if message is not None:
            self.message = message
        if code is not None:
            self.code = code
        self.details = details or []
        self.headers = headers or {}


class MalformedRequestError(AppError):
    status_code = 400
    code = "MALFORMED_REQUEST"
    message = "The request is malformed."


class AuthenticationError(AppError):
    status_code = 401
    code = "AUTHENTICATION_FAILED"
    message = "Authentication failed."

    def __init__(self, message: str | None = None, *, code: str | None = None) -> None:
        super().__init__(message, code=code, headers={"WWW-Authenticate": "Bearer"})


class AuthorizationError(AppError):
    status_code = 403
    code = "FORBIDDEN"
    message = "You do not have permission to perform this action."


class ResourceNotFoundError(AppError):
    status_code = 404
    code = "RESOURCE_NOT_FOUND"
    message = "The requested resource was not found."


class DuplicateResourceError(AppError):
    status_code = 409
    code = "DUPLICATE_RESOURCE"
    message = "The resource already exists."


class ConcurrencyConflictError(AppError):
    status_code = 409
    code = "CONCURRENCY_CONFLICT"
    message = "The resource is being modified by another request."


class IdempotencyKeyReusedError(AppError):
    status_code = 409
    code = "IDEMPOTENCY_KEY_REUSED"
    message = "This idempotency key was already used with a different message."


class PayloadTooLargeError(AppError):
    status_code = 413
    code = "PAYLOAD_TOO_LARGE"
    message = "The request body is too large."


class UnsupportedMediaTypeError(AppError):
    status_code = 415
    code = "UNSUPPORTED_MEDIA_TYPE"
    message = "The media type is not supported."


class RequestValidationAppError(AppError):
    status_code = 422
    code = "VALIDATION_ERROR"
    message = "The request contains invalid data."


class DocumentValidationError(AppError):
    status_code = 400
    code = "DOCUMENT_INVALID_FORMAT"
    message = "The uploaded file is not a valid PDF."


class DocumentEncryptedError(DocumentValidationError):
    status_code = 422
    code = "DOCUMENT_ENCRYPTED"
    message = "Password-protected PDFs are not supported. Remove the password and upload again."


class DocumentTooManyPagesError(DocumentValidationError):
    status_code = 422
    code = "DOCUMENT_TOO_MANY_PAGES"
    message = "The PDF has more pages than the allowed maximum."


class DocumentParsingError(AppError):
    status_code = 422
    code = "DOCUMENT_PARSING_FAILED"
    message = "The document could not be processed."


class IngestionError(AppError):
    status_code = 500
    code = "INGESTION_FAILED"
    message = "Document ingestion failed."


class RetrievalError(AppError):
    status_code = 500
    code = "RETRIEVAL_FAILED"
    message = "Retrieval failed."


class RetrievalTimeoutError(RetrievalError):
    status_code = 504
    code = "RETRIEVAL_TIMEOUT"
    message = "Retrieval took too long."


class AgentExecutionError(AppError):
    status_code = 500
    code = "AGENT_EXECUTION_FAILED"
    message = "The agent could not complete the request."


class ModelProviderError(AppError):
    status_code = 502
    code = "MODEL_PROVIDER_ERROR"
    message = "The model provider returned an error."


class RateLimitExceededError(AppError):
    status_code = 429
    code = "RATE_LIMIT_EXCEEDED"
    message = "Too many requests. Please try again later."

    def __init__(self, retry_after_s: int, message: str | None = None) -> None:
        super().__init__(message, headers={"Retry-After": str(max(1, retry_after_s))})
        self.retry_after_s = max(1, retry_after_s)


class ExternalServiceUnavailableError(AppError):
    status_code = 503
    code = "SERVICE_UNAVAILABLE"
    message = "A required service is temporarily unavailable."


class UpstreamTimeoutError(AppError):
    status_code = 504
    code = "UPSTREAM_TIMEOUT"
    message = "An upstream service timed out."


def _response(
    status_code: int,
    code: str,
    message: str,
    details: list[ErrorDetail] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body = ErrorResponse(
        error=ErrorBody(
            code=code, message=message, details=details or [], request_id=get_request_id()
        )
    )
    out_headers = dict(headers or {})
    request_id = get_request_id()
    if request_id:
        # The 500 handler runs outside the request-id middleware, so set the header here too.
        out_headers.setdefault(REQUEST_ID_HEADER, request_id)
    return JSONResponse(body.model_dump(), status_code=status_code, headers=out_headers)


async def _app_error_handler(_: Request, exc: Exception) -> JSONResponse:
    exc = cast(AppError, exc)  # registered only for AppError
    if exc.status_code >= 500:
        log.error("app_error", code=exc.code, status=exc.status_code, exc_info=exc)
    return _response(exc.status_code, exc.code, exc.message, exc.details, exc.headers)


async def _validation_handler(_: Request, exc: Exception) -> JSONResponse:
    exc = cast(RequestValidationError, exc)  # registered only for this type
    details: list[ErrorDetail] = []
    for err in exc.errors():
        loc = [str(p) for p in err["loc"] if p not in ("body", "query", "path", "header")]
        # Deliberately omit err["input"] and err["ctx"]: they can contain submitted secrets.
        details.append(
            ErrorDetail(field=".".join(loc) or None, code=str(err["type"]), message=err["msg"])
        )
    return _response(422, "VALIDATION_ERROR", "The request contains invalid data.", details)


_HTTP_CODES: dict[int, tuple[str, str]] = {
    400: ("MALFORMED_REQUEST", "The request is malformed."),
    401: ("AUTHENTICATION_FAILED", "Authentication failed."),
    403: ("FORBIDDEN", "You do not have permission to perform this action."),
    404: ("RESOURCE_NOT_FOUND", "The requested resource was not found."),
    405: ("METHOD_NOT_ALLOWED", "The HTTP method is not allowed for this resource."),
    413: ("PAYLOAD_TOO_LARGE", "The request body is too large."),
    415: ("UNSUPPORTED_MEDIA_TYPE", "The media type is not supported."),
}


async def _http_exception_handler(_: Request, exc: Exception) -> JSONResponse:
    exc = cast(StarletteHTTPException, exc)  # registered only for this type
    code, message = _HTTP_CODES.get(exc.status_code, ("HTTP_ERROR", "The request failed."))
    headers: dict[str, str] | None = dict(exc.headers) if exc.headers else None
    return _response(exc.status_code, code, message, headers=headers)


async def _unhandled_handler(_: Request, exc: Exception) -> JSONResponse:
    log.error("unhandled_exception", exc_info=exc)
    return _response(500, "INTERNAL_ERROR", "An internal error occurred.")


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, _app_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_handler)
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
    app.add_exception_handler(Exception, _unhandled_handler)


def error_responses(*codes: int) -> dict[int | str, dict[str, Any]]:
    """OpenAPI `responses=` helper documenting the shared error contract."""
    return {code: {"model": ErrorResponse} for code in codes}
