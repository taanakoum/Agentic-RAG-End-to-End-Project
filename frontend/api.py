"""HTTP client for the FastAPI backend (the ONLY thing the Streamlit app talks to).

Responsibilities: bearer tokens, one transparent refresh on 401, typed errors that carry the
backend's stable error code and request id, and the chat SSE stream. No Streamlit imports: it is
tested directly against a live server.
"""

from __future__ import annotations

import contextlib
import json
import threading
import uuid
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from typing import Any

import httpx

from frontend.config import FrontendConfig

JSON = dict[str, Any]


class ApiError(Exception):
    """An error reported by the backend (or a failure to reach it)."""

    def __init__(
        self,
        *,
        status: int,
        code: str,
        message: str,
        details: list[JSON] | None = None,
        request_id: str | None = None,
        retry_after_s: int | None = None,
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.status = status
        self.code = code
        self.message = message
        self.details = details or []
        self.request_id = request_id
        self.retry_after_s = retry_after_s


class SessionExpired(ApiError):
    """The refresh token no longer works: the user has to sign in again."""


@dataclass
class Tokens:
    access: str
    refresh: str


@dataclass(frozen=True)
class LoginResult:
    mfa_required: bool = False
    mfa_token: str | None = None


def _error_from(response: httpx.Response) -> ApiError:
    request_id = response.headers.get("x-request-id")
    retry_after = response.headers.get("retry-after")
    try:
        body = response.json()
        error = body["error"]
        return ApiError(
            status=response.status_code,
            code=str(error["code"]),
            message=str(error.get("message", "")),
            details=[d for d in error.get("details", []) if isinstance(d, dict)],
            request_id=str(error.get("request_id") or request_id or "") or None,
            retry_after_s=int(retry_after) if retry_after and retry_after.isdigit() else None,
        )
    except (ValueError, KeyError, TypeError):
        # a proxy page, an empty body, ...: never show it, just classify it
        return ApiError(
            status=response.status_code,
            code="UNEXPECTED_RESPONSE",
            message="The server sent an unexpected response.",
            request_id=request_id,
        )


# A 401 normally means the access token is bad; but the backend also answers 401 for a wrong
# second-factor code or password. Only token problems may end the session or trigger a refresh.
TOKEN_PROBLEMS = frozenset(
    {"INVALID_TOKEN", "SESSION_REVOKED", "AUTHENTICATION_REQUIRED", "AUTHENTICATION_FAILED"}
)


def token_problem(response: httpx.Response) -> bool:
    if response.status_code != 401:
        return False
    return _error_from(response).code in TOKEN_PROBLEMS | {"UNEXPECTED_RESPONSE"}


def iter_sse(lines: Iterable[str]) -> Iterator[JSON]:
    """Decode server-sent events (`event:`/`data:` frames, `:` comments are keep-alives)."""
    data: list[str] = []
    for line in lines:
        if line == "":
            if data:
                yield json.loads("\n".join(data))
            data = []
        elif line.startswith("data:"):
            data.append(line[5:].removeprefix(" "))
    if data:  # a final frame without the closing blank line
        yield json.loads("\n".join(data))


class ApiClient:
    def __init__(
        self,
        config: FrontendConfig | None = None,
        *,
        tokens: Tokens | None = None,
        end_user_ip: Callable[[], str | None] | None = None,
    ) -> None:
        self.config = config or FrontendConfig.from_env()
        self.tokens = tokens
        # Read at request time: one client can serve reruns (and a remembered sign-in) of
        # different browser connections.
        self._end_user_ip = end_user_ip
        self._http = httpx.Client(base_url=self.config.api_base_url, timeout=self.config.timeout_s)
        self._refresh_lock = threading.Lock()

    # ------------------------------------------------------------------ plumbing

    def close(self) -> None:
        self._http.close()

    @property
    def signed_in(self) -> bool:
        return self.tokens is not None

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = dict(extra or {})
        if self.tokens:
            headers["Authorization"] = f"Bearer {self.tokens.access}"
        return headers

    def _edge_headers(self, headers: dict[str, str] | None) -> dict[str, str] | None:
        """Add the end user's address for the API's per-IP limits (only with the shared secret)."""
        secret = self.config.client_ip_secret
        ip = self._end_user_ip() if (secret and self._end_user_ip) else None
        if not ip:
            return headers
        return {**(headers or {}), "X-Frontend-Secret": secret, "X-End-User-IP": ip}

    def _send(
        self,
        method: str,
        path: str,
        *,
        auth: bool,
        timeout: float | None = None,
        headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        try:
            return self._http.request(
                method,
                path,
                headers=self._edge_headers(self._headers(headers) if auth else headers),
                timeout=timeout or self.config.timeout_s,
                **kwargs,
            )
        except httpx.TimeoutException as exc:
            raise ApiError(
                status=504, code="NETWORK_TIMEOUT", message="The server took too long to answer."
            ) from exc
        except httpx.TransportError as exc:
            raise ApiError(
                status=503, code="NETWORK_ERROR", message="The server could not be reached."
            ) from exc

    def _refresh(self, stale_access: str) -> bool:
        """Swap the token pair once; concurrent callers share the result."""
        with self._refresh_lock:
            if self.tokens is None:
                return False
            if self.tokens.access != stale_access:  # another call already refreshed
                return True
            response = self._send(
                "POST", "/auth/refresh", auth=False, json={"refresh_token": self.tokens.refresh}
            )
            if response.status_code != 200:
                self.tokens = None
                return False
            body = response.json()
            self.tokens = Tokens(access=body["access_token"], refresh=body["refresh_token"])
            return True

    def request(
        self,
        method: str,
        path: str,
        *,
        auth: bool = True,
        expected: tuple[int, ...] = (200, 201, 202, 204),
        timeout: float | None = None,
        headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        if auth and self.tokens is None:
            raise SessionExpired(
                status=401, code="AUTHENTICATION_REQUIRED", message="Please sign in."
            )
        stale = self.tokens.access if self.tokens else ""
        response = self._send(method, path, auth=auth, timeout=timeout, headers=headers, **kwargs)
        if auth and token_problem(response):
            if self._refresh(stale):
                response = self._send(
                    method, path, auth=auth, timeout=timeout, headers=headers, **kwargs
                )
            if token_problem(response):
                self.tokens = None
                error = _error_from(response)
                raise SessionExpired(
                    status=401,
                    code=error.code,
                    message="Your session has expired. Please sign in again.",
                    request_id=error.request_id,
                )
        if response.status_code not in expected:
            raise _error_from(response)
        return response

    def _json(self, method: str, path: str, **kwargs: Any) -> Any:
        return self.request(method, path, **kwargs).json()

    # ------------------------------------------------------------------ auth

    def register(
        self,
        email: str,
        password: str,
        display_name: str | None = None,
        invite_code: str | None = None,
    ) -> JSON:
        body: JSON = {"email": email, "password": password}
        if display_name:
            body["display_name"] = display_name
        if invite_code:
            body["invite_code"] = invite_code
        return self._json("POST", "/auth/register", auth=False, json=body)

    def _take_tokens(self, body: JSON) -> LoginResult:
        if body.get("mfa_required"):
            return LoginResult(mfa_required=True, mfa_token=body.get("mfa_token"))
        self.tokens = Tokens(access=body["access_token"], refresh=body["refresh_token"])
        return LoginResult()

    def login(self, email: str, password: str) -> LoginResult:
        body = self._json(
            "POST", "/auth/login", auth=False, json={"email": email, "password": password}
        )
        return self._take_tokens(body)

    def verify_mfa(self, mfa_token: str, code: str) -> LoginResult:
        body = self._json(
            "POST", "/auth/mfa/verify", auth=False, json={"mfa_token": mfa_token, "code": code}
        )
        return self._take_tokens(body)

    def logout(self) -> None:
        """Always ends locally, even when the server cannot be reached."""
        tokens, self.tokens = self.tokens, None
        if tokens is None:
            return
        with contextlib.suppress(ApiError):
            self._send("POST", "/auth/logout", auth=False, json={"refresh_token": tokens.refresh})

    def me(self) -> JSON:
        return self._json("GET", "/auth/me")

    def auth_sessions(self) -> list[JSON]:
        return self._json("GET", "/auth/sessions")

    def revoke_auth_session(self, session_id: str) -> None:
        self.request("DELETE", f"/auth/sessions/{session_id}")

    def change_password(self, current: str, new: str) -> None:
        self.request(
            "POST", "/auth/password/change", json={"current_password": current, "new_password": new}
        )

    def forgot_password(self, email: str) -> None:
        """Looks successful whatever the account state (the server never reveals it)."""
        self.request("POST", "/auth/password/forgot", auth=False, json={"email": email})

    def reset_password(self, token: str, new_password: str) -> None:
        self.request(
            "POST",
            "/auth/password/reset",
            auth=False,
            json={"token": token, "new_password": new_password},
        )

    def request_email_verification(self) -> None:
        self.request("POST", "/auth/email/verify/request")

    def verify_email(self, token: str) -> None:
        self.request("POST", "/auth/email/verify", json={"token": token})

    def mfa_status(self) -> JSON:
        return self._json("GET", "/auth/mfa/status")

    def mfa_setup(self) -> JSON:
        return self._json("POST", "/auth/mfa/setup")

    def mfa_confirm(self, code: str) -> list[str]:
        body = self._json("POST", "/auth/mfa/confirm", json={"code": code})
        return [str(c) for c in body["recovery_codes"]]

    def mfa_disable(self, password: str, code: str) -> None:
        self.request("POST", "/auth/mfa/disable", json={"password": password, "code": code})

    def delete_account(self, password: str, code: str | None = None) -> None:
        body: JSON = {"password": password}
        if code:
            body["code"] = code
        self.request("DELETE", "/auth/account", json=body)

    # ------------------------------------------------------------------ conversations

    def create_session(self, title: str | None = None, agent_config: JSON | None = None) -> JSON:
        body: JSON = {}
        if title:
            body["title"] = title
        if agent_config:
            body["agent_config"] = agent_config
        return self._json("POST", "/sessions", json=body)

    def get_session(self, session_id: str) -> JSON:
        return self._json("GET", f"/sessions/{session_id}")

    def list_sessions(self, limit: int = 50, offset: int = 0) -> JSON:
        return self._json("GET", "/sessions", params={"limit": limit, "offset": offset})

    def update_session(
        self, session_id: str, *, title: str | None = None, agent_config: JSON | None = None
    ) -> JSON:
        body: JSON = {}
        if title is not None:
            body["title"] = title
        if agent_config is not None:
            body["agent_config"] = agent_config
        return self._json("PATCH", f"/sessions/{session_id}", json=body)

    def delete_session(self, session_id: str) -> None:
        self.request("DELETE", f"/sessions/{session_id}")

    def messages(self, session_id: str) -> list[JSON]:
        """The whole history, oldest first (paged)."""
        items: list[JSON] = []
        after = 0
        while True:
            page = self._json(
                "GET",
                f"/sessions/{session_id}/messages",
                params={"after_seq": after, "limit": 200},
            )
            items.extend(page["items"])
            if not page["has_more"] or not page["items"]:
                return items
            after = page["items"][-1]["seq"]

    # ------------------------------------------------------------------ chat

    @staticmethod
    def new_idempotency_key() -> str:
        return f"ui-{uuid.uuid4().hex}"

    def chat_stream(self, session_id: str, message: str, idempotency_key: str) -> Iterator[JSON]:
        """Events of one streamed turn. Repeating the call with the SAME key replays, never
        re-asks. A stream that ends without `complete`/`error` yields a synthetic retryable error.
        """
        if self.tokens is None:
            raise SessionExpired(
                status=401, code="AUTHENTICATION_REQUIRED", message="Please sign in."
            )
        yield from self._stream_once(session_id, message, idempotency_key, retry_auth=True)

    def _stream_once(
        self, session_id: str, message: str, key: str, *, retry_auth: bool
    ) -> Iterator[JSON]:
        assert self.tokens is not None  # noqa: S101
        stale = self.tokens.access
        finished = False
        try:
            with self._http.stream(
                "POST",
                "/chat/stream",
                json={"session_id": session_id, "message": message},
                headers=self._edge_headers(self._headers({"Idempotency-Key": key})),
                timeout=self.config.stream_timeout_s,
            ) as response:
                if response.status_code == 401 and retry_auth:
                    response.read()
                    if self._refresh(stale):
                        yield from self._stream_once(session_id, message, key, retry_auth=False)
                        return
                if response.status_code == 401:
                    self.tokens = None
                    raise SessionExpired(
                        status=401,
                        code="AUTHENTICATION_REQUIRED",
                        message="Your session has expired. Please sign in again.",
                    )
                if response.status_code != 200:
                    response.read()
                    raise _error_from(response)
                for event in iter_sse(response.iter_lines()):
                    yield event
                    if event.get("type") in {"complete", "error"}:
                        finished = True
        except httpx.TimeoutException as exc:
            raise ApiError(
                status=504, code="NETWORK_TIMEOUT", message="The server took too long to answer."
            ) from exc
        except httpx.TransportError as exc:
            if finished:
                return
            raise ApiError(
                status=503, code="STREAM_INTERRUPTED", message="The connection was interrupted."
            ) from exc
        if not finished:
            yield {
                "type": "error",
                "code": "STREAM_INTERRUPTED",
                "message": "The answer stream ended early.",
                "retryable": True,
            }

    def send_feedback(self, message_id: str, rating: str, comment: str | None = None) -> JSON:
        body: JSON = {"message_id": message_id, "rating": rating}
        if comment:
            body["comment"] = comment
        return self._json("POST", "/feedback", json=body)

    def clear_feedback(self, message_id: str) -> None:
        self.request("DELETE", f"/feedback/{message_id}")

    # ------------------------------------------------------------------ documents

    def upload(self, filename: str, data: bytes, content_type: str | None = None) -> JSON:
        file = (filename, data, content_type) if content_type else (filename, data)
        return self._json(
            "POST",
            "/documents/upload",
            files={"file": file},
            timeout=self.config.upload_timeout_s,
        )

    def list_documents(self, limit: int = 100, offset: int = 0) -> JSON:
        return self._json("GET", "/documents", params={"limit": limit, "offset": offset})

    def get_document(self, document_id: str) -> JSON:
        return self._json("GET", f"/documents/{document_id}")

    def retry_ingestion(self, job_id: str) -> JSON:
        return self._json("POST", f"/ingestion/{job_id}/retry")

    def reindex(self, document_id: str) -> JSON:
        return self._json("POST", f"/documents/{document_id}/reindex")

    def delete_document(self, document_id: str) -> None:
        self.request("DELETE", f"/documents/{document_id}")

    # ------------------------------------------------------------------ memory

    def preferences(self) -> JSON:
        return self._json("GET", "/users/me/preferences")

    def update_preferences(self, **fields: Any) -> JSON:
        return self._json("PATCH", "/users/me/preferences", json=fields)

    def list_memories(self, limit: int = 100, offset: int = 0) -> JSON:
        return self._json("GET", "/memory", params={"limit": limit, "offset": offset})

    def create_memory(self, content: str, kind: str = "semantic", importance: int = 3) -> JSON:
        return self._json(
            "POST", "/memory", json={"content": content, "kind": kind, "importance": importance}
        )

    def update_memory(self, memory_id: str, **fields: Any) -> JSON:
        return self._json("PATCH", f"/memory/{memory_id}", json=fields)

    def delete_memory(self, memory_id: str) -> None:
        self.request("DELETE", f"/memory/{memory_id}")

    def reset_memory(self) -> JSON:
        return self._json("POST", "/memory/reset", json={"confirm": True})
