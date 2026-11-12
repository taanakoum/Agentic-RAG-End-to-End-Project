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


def _verify_email(me: dict[str, object]) -> None:
    if me.get("email_verified"):
        return
    st.subheader("Verify your email")
    st.caption("Some features need a verified address. We email you a one-time code.")
    if st.button("Send verification email", key="send_verification"):
        try:
            state.api().request_email_verification()
        except ApiError as exc:
            state.fail(exc)
        else:
            st.info("Sent. In development the message is a file in data/outbox/.")
    with st.form("verify_email", clear_on_submit=True):
        token = st.text_input("Code from the email", key="verify_token")
        submitted = st.form_submit_button("Verify")
    if not submitted:
        return
    if not (token or "").strip():
        st.error("Please paste the code from the email.")
        return
    try:
        state.api().verify_email(token.strip())
        st.session_state["user"] = state.api().me()
    except ApiError as exc:
        state.fail(exc)
    else:
        st.success("Your email is verified.")
        st.rerun()


def _qr_png(uri: str) -> bytes:
    import io

    import segno

    buffer = io.BytesIO()
    segno.make(uri).save(buffer, kind="png", scale=5, border=2)
    return buffer.getvalue()


def _two_factor() -> None:
    st.subheader("Two-factor authentication")
    try:
        info = state.api().mfa_status()
    except ApiError as exc:
        state.fail(exc)
        return
    shown_codes: list[str] | None = st.session_state.pop("recovery_codes", None)
    if shown_codes:
        st.success("Two-factor authentication is on. Save these recovery codes now.")
        st.code("\n".join(shown_codes))
        st.caption("Each code works once if you lose your authenticator. They are shown only now.")
    if info["enabled"]:
        st.caption(f"On. Recovery codes left: {info['recovery_codes_remaining']}.")
        with st.form("mfa_disable", clear_on_submit=True):
            password = st.text_input("Password", type="password", key="mfa_off_password")
            code = st.text_input("Current code or a recovery code", key="mfa_off_code")
            submitted = st.form_submit_button("Turn off two-factor authentication")
        if submitted:
            if not password or not (code or "").strip():
                st.error("Enter your password and a code.")
                return
            try:
                state.api().mfa_disable(password, code.strip())
            except ApiError as exc:
                state.fail(exc)
            else:
                st.rerun()
        return
    pending: dict[str, str] | None = st.session_state.get("mfa_pending")
    if pending is None:
        st.caption("Off. Protects your account with a code from an authenticator app.")
        if st.button("Set up two-factor authentication", key="mfa_start"):
            try:
                st.session_state["mfa_pending"] = state.api().mfa_setup()
            except ApiError as exc:
                state.fail(exc)
            else:
                st.rerun()
        return
    st.caption(
        "Scan the QR code with an authenticator app (or type the secret), then enter a code."
    )
    st.image(_qr_png(pending["otpauth_uri"]), width=220)
    st.code(pending["secret"])
    with st.form("mfa_confirm", clear_on_submit=True):
        code = st.text_input("6-digit code", key="mfa_on_code")
        submitted = st.form_submit_button("Turn on")
    if submitted:
        if not (code or "").strip():
            st.error("Enter the code from the app.")
            return
        try:
            st.session_state["recovery_codes"] = state.api().mfa_confirm(code.strip())
        except ApiError as exc:
            state.fail(exc)
        else:
            st.session_state.pop("mfa_pending", None)
            st.rerun()


def _delete_account() -> None:
    with st.expander("Delete my account"):
        st.warning("This permanently deletes your account, documents, conversations and memories.")
        with st.form("delete_account"):
            password = st.text_input("Password", type="password", key="delete_password")
            code = st.text_input("Two-factor code (if turned on)", key="delete_code")
            sure = st.checkbox("I understand this cannot be undone", key="delete_sure")
            submitted = st.form_submit_button("Delete my account")
        if not submitted:
            return
        if not sure or not password:
            st.error("Enter your password and tick the box.")
            return
        try:
            state.api().delete_account(password, (code or "").strip() or None)
        except ApiError as exc:
            presentation = state.fail(exc)
            state.field_error(presentation, "password")
        else:
            state.sign_out("Your account was deleted.", tell_server=False)
            st.rerun()


def render() -> None:
    st.title("Account")
    me = state.user() or {}
    st.markdown(f"**{me.get('display_name') or me.get('email', '')}**")
    st.caption(
        f"{me.get('email', '')} · email "
        f"{'verified' if me.get('email_verified') else 'not verified yet'}"
    )
    _verify_email(me)
    _two_factor()
    _devices()
    _password()
    _delete_account()
