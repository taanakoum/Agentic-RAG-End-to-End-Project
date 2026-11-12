"""Per-browser-session state. Tokens live ONLY in `st.session_state` (server memory of this
Streamlit session): never in the URL, cookies or browser storage. A page reload starts a new
Streamlit session, so the user signs in again: the documented price of not storing tokens in the
browser (docs/spec/02).
"""

from __future__ import annotations

from typing import Any

import streamlit as st

from frontend.api import ApiClient, ApiError, SessionExpired
from frontend.config import FrontendConfig
from frontend.errors import Presentation, describe

_PRESERVE_ON_SIGN_OUT = {"notice"}


def api() -> ApiClient:
    client = st.session_state.get("api")
    if client is None:
        client = ApiClient(st.session_state.get("config") or FrontendConfig.from_env())
        st.session_state["api"] = client
    return client


def user() -> dict[str, Any] | None:
    return st.session_state.get("user")


def sign_out(notice: str | None = None, *, tell_server: bool = True) -> None:
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
        st.warning(text)
    else:
        st.error(text)


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
