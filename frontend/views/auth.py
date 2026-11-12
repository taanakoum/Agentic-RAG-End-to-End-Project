"""Sign-in, registration, the second factor and password reset."""

from __future__ import annotations

import streamlit as st

from frontend import state
from frontend.api import ApiError
from frontend.errors import Presentation, describe

FEATURES = [
    (
        ":material/verified:",
        "Answers you can check",
        "Every citation is verified against the exact passage it quotes.",
    ),
    (
        ":material/lock:",
        "Private by design",
        "Each person sees only their own documents and conversations.",
    ),
    (
        ":material/upload_file:",
        "Your formats",
        "PDF, Word, Markdown, CSV and text, with OCR for scanned pages.",
    ),
]


def _finish_login() -> None:
    try:
        st.session_state["user"] = state.api().me()
    except ApiError as exc:
        state.fail(exc)
        return
    st.session_state.pop("mfa_token", None)
    if st.session_state.pop("keep_pending", False):
        state.keep_signed_in()
    st.rerun()


def _sign_in_form() -> None:
    problem: Presentation | None = None
    with st.form("sign_in", border=False):
        email = st.text_input("Email", key="login_email", placeholder="you@example.com")
        password = st.text_input("Password", type="password", key="login_password")
        keep = st.checkbox(
            "Keep me signed in on this device",
            key="login_keep",
            help="Stay signed in after a page reload for up to 7 days (12 hours without use). "
            "Your access tokens stay on the server; sign out to end it everywhere.",
        )
        submitted = st.form_submit_button("Sign in", type="primary", use_container_width=True)
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
    st.session_state["keep_pending"] = bool(keep)
    if result.mfa_required:
        st.session_state["mfa_token"] = result.mfa_token
        st.rerun()
    _finish_login()


def _forgot_password() -> None:
    with st.expander("Forgot your password?", icon=":material/key:"):
        with st.form("forgot", clear_on_submit=False, border=False):
            email = st.text_input("Your email", key="forgot_email")
            asked = st.form_submit_button("Email me a reset code")
        if asked:
            if not (email or "").strip():
                st.error("Please enter your email.")
            else:
                try:
                    state.api().forgot_password(email.strip())
                except ApiError as exc:
                    state.show(describe(exc))
                else:  # the same message whether or not the account exists
                    st.info("If that account exists, a reset code was sent.")
        with st.form("reset", clear_on_submit=True, border=False):
            token = st.text_input("Reset code from the email", key="reset_token")
            new = st.text_input("New password", type="password", key="reset_password")
            done = st.form_submit_button("Set the new password")
        if done:
            if not (token or "").strip() or not new:
                st.error("Enter the code and the new password.")
                return
            try:
                state.api().reset_password(token.strip(), new)
            except ApiError as exc:
                presentation = describe(exc)
                state.show(presentation)
                state.field_error(presentation, "new_password")
            else:
                st.success("Password changed. You can sign in now.")


def _mfa_form() -> None:
    st.info(
        "Enter the 6-digit code from your authenticator app, or a recovery code.",
        icon=":material/phonelink_lock:",
    )
    with st.form("mfa", border=False):
        code = st.text_input("Verification code", key="mfa_code", max_chars=32)
        submitted = st.form_submit_button("Verify", type="primary")
    if st.button("Back to sign in", key="mfa_back", type="tertiary"):
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
    with st.form("register", border=False):
        email = st.text_input("Email", key="reg_email", placeholder="you@example.com")
        name = st.text_input("Display name (optional)", key="reg_name")
        password = st.text_input(
            "Password",
            type="password",
            key="reg_password",
            help="At least 12 characters; common or breached passwords are refused.",
        )
        confirm = st.text_input("Repeat password", type="password", key="reg_confirm")
        submitted = st.form_submit_button(
            "Create account", type="primary", use_container_width=True
        )
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


def _pitch() -> None:
    st.markdown("### Ask your documents. Get answers you can verify.")
    st.write(
        "Upload handbooks, contracts, reports or notes, then ask in plain language. "
        "Every answer comes with the exact passages it is based on."
    )
    st.space("small")
    for icon, title, text in FEATURES:
        st.markdown(f"{icon} **{title}**  \n{text}")


def render() -> None:
    st.title("Agentic RAG")
    st.caption("Ask questions about your own documents, with sources you can check.")
    notice = st.session_state.pop("notice", None)
    if notice:
        st.info(notice, icon=":material/info:")
    left, right = st.columns([5, 4], gap="large")
    with left:
        _pitch()
    with right, st.container(border=True):
        if st.session_state.get("mfa_token"):
            _mfa_form()
            return
        sign_in, register = st.tabs(["Sign in", "Create account"])
        with sign_in:
            _sign_in_form()
            _forgot_password()
        with register:
            _register_form()
