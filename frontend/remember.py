"""Opt-in "keep me signed in on this device" (KI-041), without putting tokens in the browser.

The API tokens never leave the Streamlit server. When the person ticks the box, the server keeps
their (already signed-in) API client in this process-local store under a random handle, and the
browser gets only that handle in a cookie (`SameSite=Strict`, `Secure` over HTTPS). A reload reads
the cookie and resumes the session. Properties:

* the handle is not an API credential: it only works against this Streamlit server process, and
  only until it expires (idle and absolute limits) or the person signs out (server-side revocation);
* only a hash of the handle is stored, so a memory dump of the store does not yield usable cookies;
* a server restart forgets everything (people sign in again), which is the safe failure;
* the cookie is set by script, so it is readable by script on this origin; Streamlit renders user
  and model text as escaped markdown (no raw HTML, images never loaded), which keeps that
  acceptable. Off by default.
"""

from __future__ import annotations

import hashlib
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any

COOKIE = "rag_keep"
IDLE_S = 12 * 3600  # forgotten after 12 h without use
ABSOLUTE_S = 7 * 24 * 3600  # and after 7 days whatever happens


@dataclass
class _Entry:
    client: Any  # frontend.api.ApiClient (kept untyped to avoid an import cycle)
    user: dict[str, Any]
    created: float
    last_seen: float


_store: dict[str, _Entry] = {}
_lock = threading.Lock()


def _key(handle: str) -> str:
    return hashlib.sha256(handle.encode()).hexdigest()


def _expired(entry: _Entry, now: float) -> bool:
    return now - entry.last_seen > IDLE_S or now - entry.created > ABSOLUTE_S


def remember(client: Any, user: dict[str, Any], *, now: float | None = None) -> str:
    """Store the signed-in client; return the handle for the cookie."""
    moment = time.time() if now is None else now
    handle = secrets.token_urlsafe(32)
    with _lock:
        _purge(moment)
        _store[_key(handle)] = _Entry(client, dict(user), moment, moment)
    return handle


def recall(handle: str | None, *, now: float | None = None) -> tuple[Any, dict[str, Any]] | None:
    """The client and profile for a handle, or None (unknown, expired or signed out)."""
    if not handle:
        return None
    moment = time.time() if now is None else now
    with _lock:
        entry = _store.get(_key(handle))
        if entry is None:
            return None
        if _expired(entry, moment) or not getattr(entry.client, "signed_in", False):
            del _store[_key(handle)]
            return None
        entry.last_seen = moment
        return entry.client, dict(entry.user)


def forget(handle: str | None) -> None:
    if handle:
        with _lock:
            _store.pop(_key(handle), None)


def _purge(now: float) -> None:
    for key in [k for k, e in _store.items() if _expired(e, now)]:
        del _store[key]


def size() -> int:
    with _lock:
        return len(_store)


def set_cookie_script(handle: str, *, secure: bool) -> str:
    flags = f"Max-Age={ABSOLUTE_S}; Path=/; SameSite=Strict" + ("; Secure" if secure else "")
    return f"<script>document.cookie = '{COOKIE}={handle}; {flags}';</script>"


def clear_cookie_script() -> str:
    return f"<script>document.cookie = '{COOKIE}=; Max-Age=0; Path=/; SameSite=Strict';</script>"
