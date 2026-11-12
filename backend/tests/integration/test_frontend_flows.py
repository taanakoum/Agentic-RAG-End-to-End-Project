"""Streamlit app flows (AppTest + the REAL backend): chat, documents, memory, tampered state."""

from __future__ import annotations

import pytest
from streamlit.testing.v1 import AppTest

from app.core.exceptions import ModelProviderError
from frontend.api import ApiClient, ApiError
from tests.fakes import ScriptedModel
from tests.integration.agent_helpers import GOOD, answer_json
from tests.integration.frontend_helpers import (
    app_for,
    backend,
    button,
    db_scalar,
    handbook_pdf,
    model,
    reset_model,
    signed_in,
    text_of,
    upload_handbook,
    wait_completed,
)
from tests.integration.test_frontend_ui import ask
from tests.live_backend import LiveBackend
from tests.pdfs import blank_pdf

pytestmark = pytest.mark.integration

__all__ = ["backend", "model", "reset_model"]

ANSWER = "Files larger than 25 megabytes are rejected [1]."


def signed_in_app(
    backend: LiveBackend, email: str = "alice@example.com"
) -> tuple[AppTest, ApiClient]:
    client = signed_in(backend, email)
    return app_for(backend, client).run(), client


def go(at: AppTest, section: str) -> AppTest:
    at.sidebar.radio[0].set_value(section)
    return at.run()


# ------------------------------------------------------------------ chat


def test_a_question_gets_a_streamed_cited_answer_that_is_saved_and_reopens(
    backend: LiveBackend, model: ScriptedModel
) -> None:
    at, client = signed_in_app(backend)
    upload_handbook(client)
    at.run()
    assert "Ask a question about your uploaded documents." in text_of(at)

    ask(at)
    assert not at.exception, at.exception
    shown = text_of(at)
    assert ANSWER in shown
    assert any("Sources (1)" in e.label for e in at.expander)
    assert "handbook.pdf, page 1" in shown
    assert len(model.calls) == 1
    assert at.session_state["active_session"] is not None
    assert [m.name for m in at.chat_message] == ["user", "assistant"]
    assert db_scalar("SELECT count(*) FROM messages") == 2

    # a fresh browser session (new AppTest, same account) finds the conversation again
    reopened = app_for(backend, client).run()
    opener = next(b for b in reopened.sidebar.button if "maximum upload" in b.label)
    opener.click()
    reopened.run()
    assert ANSWER in text_of(reopened)


def test_an_answer_that_cannot_be_verified_is_never_shown(
    backend: LiveBackend, model: ScriptedModel
) -> None:
    at, client = signed_in_app(backend)
    upload_handbook(client)
    model.script = [answer_json("It is 1 TB [S1].", ("S1", "limit is one terabyte for everyone"))]
    ask(at)
    shown = text_of(at)
    assert "could not be verified" in shown
    assert "1 TB" not in shown  # the model's claim is not displayed
    assert at.session_state["active_session"] is not None
    assert not [e for e in at.expander if "Sources" in e.label]  # and nothing is cited


def test_rating_an_answer_is_saved_by_the_backend_and_can_be_commented(
    backend: LiveBackend,
) -> None:
    at, client = signed_in_app(backend)
    upload_handbook(client)
    ask(at)
    next(b for b in at.button if b.label == "👍").click()
    at.run()
    assert db_scalar("SELECT rating FROM message_feedback") == "up"
    next(t for t in at.text_input if t.label == "Add a comment (optional)").set_value("Clear")
    button(at, "Send comment").click()
    at.run()
    assert db_scalar("SELECT comment FROM message_feedback") == "Clear"
    next(b for b in at.button if b.label == "👎").click()
    at.run()
    assert db_scalar("SELECT rating FROM message_feedback") == "down"
    assert db_scalar("SELECT count(*) FROM message_feedback") == 1


def test_a_failing_model_offers_a_retry_that_is_a_new_request_not_a_replay(
    backend: LiveBackend, model: ScriptedModel
) -> None:
    at, client = signed_in_app(backend)
    upload_handbook(client)
    model.script = [ModelProviderError("upstream down")]
    ask(at)
    shown = text_of(at)
    assert "language model is unavailable" in shown and "upstream down" not in shown
    assert db_scalar("SELECT count(*) FROM messages WHERE role = 'assistant'") == 0
    assert "Your message: What is the maximum upload size?" in shown
    retry = button(at, "Try again")  # the failure is retryable
    model.script = [GOOD]
    retry.click()
    at.run()
    assert not at.exception, at.exception
    assert ANSWER in text_of(at)
    # the backend refuses to replay a FAILED turn under its key, so the retry is a new turn:
    # the failed question stays in the history, followed by the question and its answer
    assert db_scalar("SELECT count(*) FROM messages WHERE role = 'assistant'") == 1
    assert db_scalar("SELECT count(*) FROM chat_executions WHERE status = 'completed'") == 1
    assert db_scalar("SELECT count(*) FROM chat_executions WHERE status = 'failed'") == 1


def test_an_interrupted_stream_is_retried_with_the_same_key_and_replays(
    backend: LiveBackend, model: ScriptedModel
) -> None:
    from frontend.api import ApiClient

    at, client = signed_in_app(backend)
    upload_handbook(client)
    real = ApiClient.chat_stream
    broken = {"once": True}

    def flaky(self: ApiClient, session_id: str, message: str, key: str):  # type: ignore[no-untyped-def]
        events = real(self, session_id, message, key)
        if broken.pop("once", False):  # the connection drops after the answer was generated
            for event in events:
                if event["type"] == "status":
                    continue
                if event["type"] == "token":
                    break
                yield event
            yield {"type": "error", "code": "STREAM_INTERRUPTED", "message": "", "retryable": True}
            return
        yield from events

    ApiClient.chat_stream = flaky  # type: ignore[method-assign]
    try:
        ask(at)
        assert "connection was interrupted" in text_of(at)
        button(at, "Try again").click()
        at.run()
    finally:
        ApiClient.chat_stream = real  # type: ignore[method-assign]
    assert ANSWER in text_of(at)
    assert len(model.calls) == 1  # the replay did not ask the model again
    assert db_scalar("SELECT count(*) FROM messages") == 2  # one question, one answer


def test_renaming_and_deleting_a_conversation(backend: LiveBackend) -> None:
    at, client = signed_in_app(backend)
    upload_handbook(client)
    ask(at)
    session = at.session_state["active_session"]
    at.text_input(key=f"title_{session}").set_value("Upload limits")
    button(at, "Save settings").click()
    at.run()
    assert any("Upload limits" in b.label for b in at.sidebar.button)
    assert db_scalar("SELECT title FROM conversations") == "Upload limits"

    assert button(at, "Delete conversation").disabled  # nothing happens until confirmed
    assert db_scalar("SELECT count(*) FROM conversations WHERE deleted_at IS NULL") == 1
    at.checkbox(key=f"del_ok_{session}").check()
    at.run()
    button(at, "Delete conversation").click()
    at.run()
    assert db_scalar("SELECT count(*) FROM conversations WHERE deleted_at IS NULL") == 0
    assert at.session_state["active_session"] is None


# ------------------------------------------------------------------ documents


def test_the_documents_page_shows_status_and_failure_details(backend: LiveBackend) -> None:
    at, client = signed_in_app(backend)
    upload_handbook(client)
    failed = client.upload("empty.pdf", blank_pdf(), "application/pdf")
    wait_completed(client, failed["document"]["id"])
    go(at, "Documents")
    shown = text_of(at)
    assert "handbook.pdf" in shown and "Ready" in shown
    assert "empty.pdf" in shown and "Failed" in shown
    assert "No readable text" in shown  # the stored failure, in plain words
    assert "permanent" in shown  # and no retry is offered for a permanent failure
    assert not [b for b in at.button if b.label == "Retry"]


def test_uploading_through_the_page_and_the_duplicate_notice(backend: LiveBackend) -> None:
    at, _client = signed_in_app(backend)
    go(at, "Documents")
    assert "No documents yet" in text_of(at)
    at.file_uploader[0].upload("notes.pdf", handbook_pdf(), "application/pdf")
    at.run()
    button(at, "Upload").click()
    at.run()
    assert "Uploaded." in text_of(at)
    assert db_scalar("SELECT count(*) FROM documents") == 1
    at.file_uploader[0].upload("notes-again.pdf", handbook_pdf(), "application/pdf")
    at.run()
    button(at, "Upload").click()
    at.run()
    assert "already uploaded this exact file" in text_of(at)
    assert db_scalar("SELECT count(*) FROM documents") == 1


# ------------------------------------------------------------------ memory


def test_memory_is_opt_in_and_can_be_added_edited_and_deleted(backend: LiveBackend) -> None:
    at, _client = signed_in_app(backend)
    go(at, "Memory")
    assert at.toggle(key="memory_enabled").value is False  # off until the person opts in
    assert "Nothing is remembered yet." in text_of(at)

    at.toggle(key="memory_enabled").set_value(True).run()
    button(at, "Save memory settings").click()
    at.run()
    assert db_scalar("SELECT memory_enabled FROM user_preferences") is True

    at.text_area(key="new_memory").set_value("I prefer answers in German")
    button(at, "Add memory").click()
    at.run()
    assert db_scalar("SELECT content FROM memories") == "I prefer answers in German"
    memory_id = str(db_scalar("SELECT id FROM memories"))
    at.text_area(key=f"mem_text_{memory_id}").set_value("I prefer answers in French")
    at.run()
    button(at, "Save changes").click()
    at.run()
    assert db_scalar("SELECT content FROM memories") == "I prefer answers in French"
    button(at, "Delete").click()
    at.run()
    assert db_scalar("SELECT count(*) FROM memories WHERE deleted_at IS NULL") == 0


# ------------------------------------------------------------------ manipulated client state


def test_tampering_with_the_clients_state_cannot_show_another_users_data(
    backend: LiveBackend, model: ScriptedModel
) -> None:
    alice_client = signed_in(backend, "alice@example.com")
    upload_handbook(alice_client, "alice-private.pdf")
    alice_app = app_for(backend, alice_client).run()
    ask(alice_app, "What is the maximum upload size? (question from alice)")
    alice_session = alice_app.session_state["active_session"]
    alice_user = alice_client.me()

    bob_client = signed_in(backend, "bob@example.com")
    bob = app_for(backend, bob_client).run()
    # Bob forges the UI state: Alice's conversation id and Alice's profile
    bob.session_state["active_session"] = alice_session
    bob.session_state["user"] = alice_user
    bob.run()
    shown = text_of(bob)
    assert not bob.exception
    assert "question from alice" not in shown and "Files larger than 25" not in shown
    assert bob.session_state["active_session"] is None  # unknown to the server: dropped

    go(bob, "Documents")
    assert "alice-private.pdf" not in text_of(bob)  # the token, not the forged profile, decides
    assert "No documents yet" in text_of(bob)

    # and a message sent into Alice's conversation id is refused by the backend
    model.calls.clear()
    with pytest.raises(ApiError) as denied:
        list(bob_client.chat_stream(alice_session, "hello", bob_client.new_idempotency_key()))
    assert denied.value.status == 404 and model.calls == []


def test_signing_out_wipes_the_previous_persons_state_before_the_next_sign_in(
    backend: LiveBackend, model: ScriptedModel
) -> None:
    at, client = signed_in_app(backend, "alice@example.com")
    upload_handbook(client)
    ask(at)
    assert at.session_state["active_session"] is not None
    button(at.sidebar, "Sign out").click()
    at.run()
    for leftover in ("active_session", "user", "ratings", "inflight"):
        assert leftover not in at.session_state, leftover
    assert not at.session_state["api"].signed_in  # a fresh, signed-out client
    signed_in(backend, "bob@example.com")  # the next person in the same browser session
    at.text_input(key="login_email").set_value("bob@example.com")
    at.text_input(key="login_password").set_value("Correct-Horse-Battery-9!")
    button(at, "Sign in").click()
    at.run()
    assert not at.exception
    assert "maximum upload" not in text_of(at) and "ANSWER" not in text_of(at)
    assert not [b for b in at.sidebar.button if "maximum upload" in b.label]
    assert "Ask a question about your uploaded documents." in text_of(at)
