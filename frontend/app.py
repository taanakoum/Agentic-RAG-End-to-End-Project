"""Streamlit entry point:  streamlit run frontend/app.py

The app talks ONLY to the FastAPI backend (FRONTEND_API_URL). It has no database, vector store or
model access, and the backend decides everything that matters (ownership, validation, limits).
"""

from __future__ import annotations

import sys
from pathlib import Path

# `streamlit run frontend/app.py` puts only frontend/ on sys.path; hosts such as Streamlit Community
# Cloud set no PYTHONPATH, so make the repository root importable before importing the package.
_ROOT = str(Path(__file__).resolve().parents[1])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import streamlit as st  # noqa: E402

from frontend import state, theme  # noqa: E402
from frontend.views import account, auth, chat, documents, memory  # noqa: E402

SECTIONS = {
    "Chat": chat.render,
    "Documents": documents.render,
    "Memory": memory.render,
    "Account": account.render,
}


def _sidebar_header(me: dict[str, object]) -> str:
    name = str(me.get("display_name") or me.get("email") or "")
    with st.sidebar:
        with st.container(border=True):
            st.caption("Signed in as")
            st.markdown(f":material/account_circle: **{name}**")
            if me.get("display_name"):
                st.caption(str(me.get("email", "")))
        section = st.radio(
            "Go to",
            list(SECTIONS),
            key="section",
            format_func=theme.nav_label,
            label_visibility="collapsed",
        )
        if st.button("Sign out", key="sign_out", icon=":material/logout:"):
            state.sign_out("You have been signed out.")
            st.rerun()
        st.divider()
    return str(section)


def main() -> None:
    st.set_page_config(
        page_title="Agentic RAG",
        page_icon=str(theme.ICON),
        layout="wide",
        initial_sidebar_state="expanded",
    )
    theme.apply()
    state.restore_session()
    if not state.api().signed_in or state.user() is None:
        auth.render()
        state.write_cookies()
        return
    section = _sidebar_header(state.user() or {})
    SECTIONS[section]()
    state.write_cookies()


main()
