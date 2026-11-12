"""Account screens of the Streamlit app against the REAL backend: two-factor setup and sign-in,
e-mail verification, password reset and account deletion (e-mails are the files the dev backend
writes). Only the language model is scripted (not used here)."""

from __future__ import annotations

import time
from typing import Any

import pyotp
import pytest
from streamlit.testing.v1 import AppTest

from app.core.config import get_settings
from frontend.api import ApiClient
from tests.helpers import read_outbox, token_from
from tests.integration.frontend_helpers import (
    PASSWORD,
    app_for,
    backend,
    button,
    db_scalar,
    model,
    reset_model,
    signed_in,
    text_of,
)
from tests.integration.test_frontend_ui import sign_in
from tests.live_backend import LiveBackend

pytestmark = pytest.mark.integration

__all__ = ["backend", "model", "reset_model"]


def latest_token(label: str, wait_s: float = 15.0) -> str:
    """The token of the newest e-mail that carries `label` (e-mails are sent after the response)."""
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        for message in reversed(read_outbox(get_settings().email_file_dir)):
            try:
                return token_from(message, label)
            except AssertionError:
                continue
        time.sleep(0.2)
    raise AssertionError(f"no e-mail with a {label!r} arrived")


def account_app(
    backend: LiveBackend, email: str = "alice@example.com"
) -> tuple[AppTest, ApiClient]:
    client = signed_in(backend, email)
    at = app_for(backend, client).run()
    at.sidebar.radio[0].set_value("Account").run()
    return at, client


def code_blocks(at: AppTest) -> list[str]:
    return [str(c.value) for c in at.code]


def test_setting_up_two_factor_in_the_ui_then_signing_in_needs_the_code(
    backend: LiveBackend,
) -> None:
    at, _client = account_app(backend)
    assert "Off. Protects your account" in text_of(at)
    button(at, "Set up two-factor authentication").click()
    at.run()
    assert not at.exception, at.exception
    secret = code_blocks(at)[0]  # shown next to the QR image
    assert len(secret) >= 16 and at.get("image")  # the QR code is rendered
    at.text_input(key="mfa_on_code").set_value("000000")
    button(at, "Turn on").click()
    at.run()
    assert "Invalid" in text_of(at) or "not correct" in text_of(at)  # a wrong code is refused

    at.text_input(key="mfa_on_code").set_value(pyotp.TOTP(secret).now())
    button(at, "Turn on").click()
    at.run()
    assert "Save these recovery codes" in text_of(at)
    assert len(code_blocks(at)[0].splitlines()) >= 8  # the recovery codes, shown once
    assert db_scalar("SELECT count(*) FROM user_mfa WHERE confirmed_at IS NOT NULL") == 1

    # a new browser session: the password alone is no longer enough
    fresh = app_for(backend).run()
    fresh = sign_in(fresh, "alice@example.com")
    assert "authenticator" in text_of(fresh) and not fresh.sidebar.radio
    fresh.text_input(key="mfa_code").set_value("123456")
    button(fresh, "Verify").click()
    fresh.run()
    assert not fresh.sidebar.radio
    # the next code (TOTP codes are single-use per step) opens the door
    fresh.text_input(key="mfa_code").set_value(
        pyotp.TOTP(secret).generate_otp(int(time.time() // 30) + 1)
    )
    button(fresh, "Verify").click()
    fresh.run()
    assert fresh.sidebar.radio, text_of(fresh)


def test_turning_two_factor_off_needs_the_password_and_a_code(backend: LiveBackend) -> None:
    at, client = account_app(backend)
    secret = client.mfa_setup()["secret"]
    client.mfa_confirm(pyotp.TOTP(secret).now())
    at = app_for(backend, client).run()
    at.sidebar.radio[0].set_value("Account").run()
    assert "Recovery codes left" in text_of(at)
    at.text_input(key="mfa_off_password").set_value(PASSWORD)
    at.text_input(key="mfa_off_code").set_value(
        pyotp.TOTP(secret).generate_otp(int(time.time() // 30) + 1)
    )
    button(at, "Turn off two-factor authentication").click()
    at.run()
    assert db_scalar("SELECT count(*) FROM user_mfa WHERE confirmed_at IS NOT NULL") == 0
    assert "Set up two-factor authentication" in [b.label for b in at.button]


def test_verifying_the_email_from_the_account_page(backend: LiveBackend) -> None:
    at, _client = account_app(backend)
    assert "not verified yet" in text_of(at)
    button(at, "Send verification email").click()
    at.run()
    token = latest_token("Verification token")
    at.text_input(key="verify_token").set_value("not-the-code")
    button(at, "Verify").click()
    at.run()
    assert "invalid or has expired" in text_of(at) or "Please correct" in text_of(at)
    at.text_input(key="verify_token").set_value(token)
    button(at, "Verify").click()
    at.run()
    assert db_scalar("SELECT email_verified_at IS NOT NULL FROM users") is True
    assert "email verified" in text_of(at)


def test_resetting_a_forgotten_password_from_the_sign_in_page(backend: LiveBackend) -> None:
    signed_in(backend, "alice@example.com")
    at = app_for(backend).run()
    at.text_input(key="forgot_email").set_value("alice@example.com")
    button(at, "Email me a reset code").click()
    at.run()
    assert "If that account exists" in text_of(at)
    token = latest_token("Reset token")

    new_password = "Brand-New-Passphrase-77!"
    at.text_input(key="reset_token").set_value(token)
    at.text_input(key="reset_password").set_value(new_password)
    button(at, "Set the new password").click()
    at.run()
    assert "Password changed" in text_of(at)
    assert "not correct" in text_of(
        sign_in(at, "alice@example.com", PASSWORD)
    )  # the old one is dead
    done = sign_in(at, "alice@example.com", new_password)
    assert done.sidebar.radio


def test_an_unknown_email_gets_the_same_reset_message(backend: LiveBackend) -> None:
    at = app_for(backend).run()
    at.text_input(key="forgot_email").set_value("nobody@example.com")
    button(at, "Email me a reset code").click()
    at.run()
    assert "If that account exists" in text_of(at)  # no hint whether the account exists


def test_deleting_the_account_asks_for_the_password_and_then_signs_out(
    backend: LiveBackend,
) -> None:
    at, _client = account_app(backend)
    at.text_input(key="delete_password").set_value("wrong-password-123")
    at.checkbox(key="delete_sure").check()
    button(at, "Delete my account").click()
    at.run()
    assert db_scalar("SELECT count(*) FROM users WHERE deleted_at IS NOT NULL") == 0  # refused
    assert at.sidebar.radio  # still signed in

    at.text_input(key="delete_password").set_value(PASSWORD)
    at.checkbox(key="delete_sure").check()
    button(at, "Delete my account").click()
    at.run()
    assert "account was deleted" in text_of(at) and not at.sidebar.radio
    shown: Any = sign_in(at, "alice@example.com")
    assert "not correct" in text_of(shown)  # the account is gone
