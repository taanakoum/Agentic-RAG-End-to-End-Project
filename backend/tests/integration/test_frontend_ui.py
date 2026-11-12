"""The Streamlit app itself (AppTest runs the real script) against the REAL backend.

Only the language model is scripted. What a person can see and do: sign in and out, register,
upload and watch a document, ask a question and get a streamed, cited, saved answer, rate it,
manage memory, and what happens when things go wrong.
"""

from __future__ import annotations

import pytest
from streamlit.testing.v1 import AppTest

from frontend.api import Tokens
from frontend.theme import nav_label
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
from tests.live_backend import LiveBackend

pytestmark = pytest.mark.integration

__all__ = ["backend", "model", "reset_model"]

QUESTION = "What is the maximum upload size?"


def sign_in(at: AppTest, email: str, password: str = PASSWORD) -> AppTest:
    at.text_input(key="login_email").set_value(email)
    at.text_input(key="login_password").set_value(password)
    button(at, "Sign in").click()
    return at.run()


def ask(at: AppTest, question: str = QUESTION) -> AppTest:
    at.chat_input[0].set_value(question)
    return at.run()


# ------------------------------------------------------------------ authentication


def test_the_first_screen_is_the_sign_in_page_and_nothing_else(backend: LiveBackend) -> None:
    at = app_for(backend).run()
    assert not at.exception
    assert at.title[0].value == "Agentic RAG"
    assert [t.label for t in at.tabs] == ["Sign in", "Create account"]
    assert not at.sidebar.radio  # no navigation, no data, before signing in


def test_missing_fields_and_wrong_credentials_are_told_apart(backend: LiveBackend) -> None:
    signed_in(backend, "alice@example.com")
    at = app_for(backend).run()
    button(at, "Sign in").click()
    at.run()
    assert "Please enter an email and a password." in text_of(at)

    at = sign_in(at, "alice@example.com", "Wrong-Password-1!")
    assert "email or password is not correct" in text_of(at)
    assert "reference:" in text_of(at)  # a support reference (request id) is shown
    assert not at.sidebar.radio and not at.exception


def test_signing_in_shows_the_app_and_signing_out_returns_to_the_start(
    backend: LiveBackend,
) -> None:
    signed_in(backend, "alice@example.com")
    at = sign_in(app_for(backend).run(), "alice@example.com")
    assert not at.exception
    nav = at.sidebar.radio[0]
    assert nav.options == [nav_label(s) for s in ("Chat", "Documents", "Memory", "Account")]
    assert nav.value == "Chat"
    assert "alice@example.com" in "".join(str(m.value) for m in at.sidebar.markdown)

    button(at.sidebar, "Sign out").click()
    at.run()
    assert at.title[0].value == "Agentic RAG" and not at.sidebar.radio
    assert "signed out" in text_of(at)
    assert "api" not in at.session_state or not at.session_state["api"].signed_in


def test_registering_in_the_ui_creates_the_account_and_signs_in(backend: LiveBackend) -> None:
    at = app_for(backend).run()
    at.text_input(key="reg_email").set_value("new.person@example.com")
    at.text_input(key="reg_password").set_value(PASSWORD)
    at.text_input(key="reg_confirm").set_value("something else")
    button(at, "Create account").click()
    at.run()
    assert "do not match" in text_of(at)
    assert db_scalar("SELECT count(*) FROM users") == 0  # nothing was sent

    at.text_input(key="reg_confirm").set_value(PASSWORD)
    button(at, "Create account").click()
    at.run()
    assert not at.exception and at.sidebar.radio  # signed in
    assert db_scalar("SELECT count(*) FROM users") == 1


def test_a_taken_email_and_a_weak_password_get_field_level_messages(backend: LiveBackend) -> None:
    signed_in(backend, "alice@example.com")
    at = app_for(backend).run()
    at.text_input(key="reg_email").set_value("alice@example.com")
    at.text_input(key="reg_password").set_value(PASSWORD)
    at.text_input(key="reg_confirm").set_value(PASSWORD)
    button(at, "Create account").click()
    at.run()
    assert "already exists" in text_of(at)

    at.text_input(key="reg_email").set_value("other@example.com")
    at.text_input(key="reg_password").set_value("short")
    at.text_input(key="reg_confirm").set_value("short")
    button(at, "Create account").click()
    at.run()
    shown = text_of(at)
    assert "Please correct the highlighted fields." in shown
    assert any(":red[" in str(c.value) or "password" in str(c.value).lower() for c in at.caption)


def test_an_expired_session_sends_the_person_back_to_sign_in_with_a_reason(
    backend: LiveBackend,
) -> None:
    client = signed_in(backend, "alice@example.com")
    at = app_for(backend, client).run()
    assert at.sidebar.radio
    client.tokens = Tokens(access="forged", refresh="forged")  # e.g. revoked elsewhere
    at.sidebar.radio[0].set_value("Documents").run()
    assert at.title[0].value == "Agentic RAG" and not at.sidebar.radio
    assert "session has expired" in text_of(at)


def test_keep_me_signed_in_remembers_the_session_server_side_and_sign_out_revokes_it(
    backend: LiveBackend,
) -> None:
    from frontend import remember

    signed_in(backend, "alice@example.com")
    before = remember.size()
    at = app_for(backend).run()
    at.checkbox(key="login_keep").check()
    at = sign_in(at, "alice@example.com")
    assert at.sidebar.radio and remember.size() == before + 1
    handle = at.session_state["keep_handle"]
    found = remember.recall(handle)
    assert found is not None and found[1]["email"] == "alice@example.com"  # resumable
    scripts = [str(h.proto.body) for h in at.get("html")]
    assert any(remember.COOKIE in body and handle in body for body in scripts)  # cookie emitted
    assert all("Bearer" not in body and "eyJ" not in body for body in scripts)  # never a token

    button(at.sidebar, "Sign out").click()
    at.run()
    assert remember.recall(handle) is None  # revoked on the server
    assert any("Max-Age=0" in str(h.proto.body) for h in at.get("html"))  # and cleared


def test_without_the_box_nothing_is_remembered(backend: LiveBackend) -> None:
    from frontend import remember

    signed_in(backend, "alice@example.com")
    before = remember.size()
    at = sign_in(app_for(backend).run(), "alice@example.com")
    assert at.sidebar.radio and remember.size() == before
    assert "keep_handle" not in at.session_state
