"""Streamlit entry point:  streamlit run frontend/app.py

The app talks ONLY to the FastAPI backend (FRONTEND_API_URL). It has no database, vector store or
model access, and the backend decides everything that matters (ownership, validation, limits).
"""

from __future__ import annotations

import streamlit as st

from frontend import state, theme
from frontend.views import account, auth, chat, documents, memory

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
            avatar, who = st.columns([1, 4], vertical_alignment="center")
            avatar.markdown(f"#### {theme.initials(name)}")
            who.markdown(f"Signed in as **{me.get('display_name') or me.get('email', '')}**")
            if me.get("display_name"):
                who.caption(str(me.get("email", "")))
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
