"""Per-browser-session state. API tokens live ONLY on the Streamlit server (`st.session_state`, and,
when the person opts in to "keep me signed in", the server-side store in `frontend/remember.py`):
never in the URL, cookies or browser storage. Without the opt-in a page reload starts a new
Streamlit session and the person signs in again (docs/spec/02).
"""

from __future__ import annotations

from typing import Any

import streamlit as st

from frontend import remember
from frontend.api import ApiClient, ApiError, SessionExpired
from frontend.config import FrontendConfig
from frontend.errors import Presentation, describe

_PRESERVE_ON_SIGN_OUT = {"notice", "clear_cookie"}


def end_user_ip() -> str | None:
    """The browser connection's address (None on localhost or without a browser context)."""
    try:
        return st.context.ip_address
    except Exception:
        return None


def wake_backend() -> None:
    """Free hosting: wait (once per browser session) for a sleeping API to start."""
    cfg = config()
    if cfg.wake_timeout_s <= 0 or st.session_state.get("backend_awake"):
        return
    import time

    import httpx

    deadline = time.monotonic() + cfg.wake_timeout_s
    message = "Starting the server. Free hosting pauses it when idle; this takes about a minute..."
    with st.spinner(message):
        while True:
            try:
                if httpx.get(f"{cfg.api_base_url}/health/live", timeout=10.0).status_code == 200:
                    st.session_state["backend_awake"] = True
                    return
            except httpx.HTTPError:
                pass
            if time.monotonic() > deadline:
                break
            time.sleep(3.0)
    st.error("The server did not start in time. Please reload the page in a minute.")
    st.stop()


def config() -> FrontendConfig:
    cfg = st.session_state.get("config")
    return cfg if isinstance(cfg, FrontendConfig) else FrontendConfig.from_env()


def api() -> ApiClient:
    client = st.session_state.get("api")
    if client is None:
        client = ApiClient(
            st.session_state.get("config") or FrontendConfig.from_env(), end_user_ip=end_user_ip
        )
        st.session_state["api"] = client
    return client


def user() -> dict[str, Any] | None:
    return st.session_state.get("user")


# ------------------------------------------------------------------ keep me signed in (opt-in)


def _cookie_handle() -> str | None:
    try:
        value = st.context.cookies.get(remember.COOKIE)
    except Exception:  # no browser context (tests, bare mode)
        return None
    return str(value) if value else None


def _https() -> bool:
    url = str(getattr(st.context, "url", "") or "")
    return url.startswith("https://")


def keep_signed_in() -> None:
    """Remember the current, signed-in session for this browser (after a successful login)."""
    me = user()
    if me is None or not api().signed_in:
        return
    st.session_state["keep_handle"] = remember.remember(api(), me)


def restore_session() -> None:
    """At the start of a run: resume a remembered session after a page reload."""
    if api().signed_in and user() is not None:
        return
    handle = _cookie_handle()
    if not handle:
        return
    found = remember.recall(handle)
    if found is None:
        st.session_state["clear_cookie"] = True  # expired or revoked: drop the stale cookie
        return
    client, profile = found
    st.session_state["api"] = client
    st.session_state["user"] = profile
    st.session_state["keep_handle"] = handle


def write_cookies() -> None:
    """Emit the cookie script for this run (set while remembered, clear after sign-out)."""
    if st.session_state.pop("clear_cookie", False):
        st.html(remember.clear_cookie_script(), unsafe_allow_javascript=True)
    handle = st.session_state.get("keep_handle")
    if handle:
        st.html(remember.set_cookie_script(handle, secure=_https()), unsafe_allow_javascript=True)


# ------------------------------------------------------------------ sign out and errors


def sign_out(notice: str | None = None, *, tell_server: bool = True) -> None:
    handle = st.session_state.get("keep_handle")
    if handle:
        remember.forget(handle)  # server-side revocation of "keep me signed in"
        st.session_state["clear_cookie"] = True
    client = st.session_state.get("api")
    if client is not None:
        if tell_server:
            client.logout()
        client.close()
    config = st.session_state.get("config")
    for key in list(st.session_state.keys()):
        if key not in _PRESERVE_ON_SIGN_OUT:
            del st.session_state[key]
    if config is not None:
        st.session_state["config"] = config
    if notice:
        st.session_state["notice"] = notice


def show(presentation: Presentation) -> None:
    text = presentation.with_reference()
    if presentation.kind in {"outage", "limit", "conflict"}:
        st.warning(text, icon=":material/warning:")
    else:
        st.error(text, icon=":material/error:")


def fail(error: ApiError) -> Presentation:
    """Show an error. An expired session ends the session and returns to the sign-in page."""
    if isinstance(error, SessionExpired):
        sign_out("Your session has expired. Please sign in again.", tell_server=False)
        st.rerun()
    presentation = describe(error)
    show(presentation)
    return presentation


def field_error(presentation: Presentation | None, name: str) -> None:
    """The message for one form field, shown right under it."""
    if presentation and name in presentation.fields:
        st.caption(f":red[{presentation.fields[name]}]")
