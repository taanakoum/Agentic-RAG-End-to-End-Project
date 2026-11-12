"""Sign-in, registration and the second factor."""

from __future__ import annotations

import streamlit as st

from frontend import state
from frontend.api import ApiError
from frontend.errors import Presentation, describe


def _finish_login() -> None:
    try:
        st.session_state["user"] = state.api().me()
    except ApiError as exc:
        state.fail(exc)
        return
    st.session_state.pop("mfa_token", None)
    st.rerun()


def _sign_in_form() -> None:
    problem: Presentation | None = None
    with st.form("sign_in"):
        email = st.text_input("Email", key="login_email")
        password = st.text_input("Password", type="password", key="login_password")
        submitted = st.form_submit_button("Sign in")
    if not submitted:
        return
    if not email.strip() or not password:
        # distinguish "you left something out" from "those credentials are wrong"
        missing = [n for n, v in (("an email", email.strip()), ("a password", password)) if not v]
        st.error("Please enter " + " and ".join(missing) + ".")
        return
    try:
        result = state.api().login(email.strip(), password)
    except ApiError as exc:
        problem = describe(exc)
        state.show(problem)
        return
    if result.mfa_required:
        st.session_state["mfa_token"] = result.mfa_token
        st.rerun()
    _finish_login()


def _mfa_form() -> None:
    st.info("Enter the 6-digit code from your authenticator app, or a recovery code.")
    with st.form("mfa"):
        code = st.text_input("Verification code", key="mfa_code")
        submitted = st.form_submit_button("Verify")
    if st.button("Back to sign in", key="mfa_back"):
        st.session_state.pop("mfa_token", None)
        st.rerun()
    if not submitted:
        return
    if not code.strip():
        st.error("Please enter the code.")
        return
    try:
        state.api().verify_mfa(st.session_state["mfa_token"], code.strip())
    except ApiError as exc:
        presentation = describe(exc)
        if presentation.kind == "auth":  # the short-lived token expired: start over
            st.session_state.pop("mfa_token", None)
        state.show(presentation)
        return
    _finish_login()


def _register_form() -> None:
    problem: Presentation | None = None
    with st.form("register"):
        email = st.text_input("Email", key="reg_email")
        name = st.text_input("Display name (optional)", key="reg_name")
        password = st.text_input("Password", type="password", key="reg_password")
        confirm = st.text_input("Repeat password", type="password", key="reg_confirm")
        submitted = st.form_submit_button("Create account")
    if not submitted:
        return
    if not email.strip() or not password:
        st.error("Please enter an email and a password.")
        return
    if password != confirm:
        st.error("The two passwords do not match.")
        return
    try:
        state.api().register(email.strip(), password, name.strip() or None)
    except ApiError as exc:
        problem = describe(exc)
        state.show(problem)
        for field in ("email", "password", "display_name"):
            state.field_error(problem, field)
        return
    st.success("Account created. Signing you in...")
    try:
        result = state.api().login(email.strip(), password)
    except ApiError as exc:
        state.show(describe(exc))
        return
    if result.mfa_required:  # a brand-new account cannot have MFA, but never assume
        st.session_state["mfa_token"] = result.mfa_token
        st.rerun()
    _finish_login()


def render() -> None:
    st.title("Agentic RAG")
    st.caption("Ask questions about your own documents, with sources you can check.")
    notice = st.session_state.pop("notice", None)
    if notice:
        st.info(notice)
    if st.session_state.get("mfa_token"):
        _mfa_form()
        return
    sign_in, register = st.tabs(["Sign in", "Create account"])
    with sign_in:
        _sign_in_form()
    with register:
        _register_form()
