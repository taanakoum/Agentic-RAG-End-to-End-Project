"""Profile, signed-in devices and password change."""

from __future__ import annotations

import streamlit as st

from frontend import state
from frontend.api import ApiError


def _devices() -> None:
    st.subheader("Where you are signed in")
    try:
        sessions = state.api().auth_sessions()
    except ApiError as exc:
        state.fail(exc)
        return
    for item in sessions:
        left, right = st.columns([5, 2])
        label = item.get("user_agent") or "Unknown device"
        left.markdown(f"{label}{' (this one)' if item['current'] else ''}")
        left.caption(f"last used {item['last_used_at']}")
        if not item["current"] and right.button("Sign out there", key=f"revoke_{item['id']}"):
            try:
                state.api().revoke_auth_session(item["id"])
            except ApiError as exc:
                state.fail(exc)
            else:
                st.rerun()


def _password() -> None:
    st.subheader("Change password")
    with st.form("change_password", clear_on_submit=True):
        current = st.text_input("Current password", type="password", key="pw_current")
        new = st.text_input("New password", type="password", key="pw_new")
        submitted = st.form_submit_button("Change password")
    if not submitted:
        return
    if not current or not new:
        st.error("Please fill in both passwords.")
        return
    try:
        state.api().change_password(current, new)
    except ApiError as exc:
        presentation = state.fail(exc)
        for field in ("current_password", "new_password"):
            state.field_error(presentation, field)
    else:
        st.success("Password changed. Your other devices were signed out.")


def render() -> None:
    st.title("Account")
    me = state.user() or {}
    st.markdown(f"**{me.get('display_name') or me.get('email', '')}**")
    st.caption(
        f"{me.get('email', '')} · email "
        f"{'verified' if me.get('email_verified') else 'not verified yet'}"
    )
    _devices()
    _password()
