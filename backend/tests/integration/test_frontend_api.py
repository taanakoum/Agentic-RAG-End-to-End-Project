"""The frontend's API client against the REAL backend (socket, PostgreSQL, Redis, Qdrant, Celery).

Only the language model is scripted. These are the contracts the Streamlit app relies on: token
refresh, error classification, the SSE stream, replay by idempotency key, and isolation.
"""

from __future__ import annotations

import socket

import pytest

from app.core.config import get_settings
from frontend.api import ApiClient, ApiError, SessionExpired, Tokens, iter_sse
from frontend.config import FrontendConfig
from frontend.errors import describe
from tests.fakes import ScriptedModel
from tests.integration.frontend_helpers import (
    PASSWORD,
    backend,
    db_scalar,
    handbook_pdf,
    model,
    new_client,
    reset_model,
    signed_in,
    upload_handbook,
)
from tests.live_backend import LiveBackend

pytestmark = pytest.mark.integration

__all__ = ["backend", "model", "reset_model"]

QUESTION = "What is the maximum upload size?"


# ------------------------------------------------------------------ SSE decoding (pure)


def test_sse_decoder_skips_keep_alives_and_handles_a_missing_final_blank_line() -> None:
    lines = [
        ": keep-alive",
        "",
        "event: token",
        'data: {"type": "token", "index": 0, "text": "Hi "}',
        "",
        "event: complete",
        'data: {"type": "complete"}',
    ]
    assert [e["type"] for e in iter_sse(lines)] == ["token", "complete"]
    assert list(iter_sse([])) == []


# ------------------------------------------------------------------ tokens and sessions


def test_register_login_me_and_a_transparent_token_refresh(backend: LiveBackend) -> None:
    client = signed_in(backend, "alice@example.com")
    assert client.me()["email"] == "alice@example.com"
    old = client.tokens
    assert old is not None
    client.tokens = Tokens(access="expired-or-forged", refresh=old.refresh)
    assert client.me()["email"] == "alice@example.com"  # 401 -> refresh -> retried, invisibly
    assert client.tokens is not None and client.tokens.refresh != old.refresh  # rotated


def test_a_dead_refresh_token_ends_the_session_cleanly(backend: LiveBackend) -> None:
    client = signed_in(backend, "alice@example.com")
    client.tokens = Tokens(access="forged", refresh="also-forged")
    with pytest.raises(SessionExpired):
        client.me()
    assert not client.signed_in
    with pytest.raises(SessionExpired):  # and it keeps failing fast, without calling the server
        client.list_sessions()


def test_logout_revokes_the_server_side_session(backend: LiveBackend) -> None:
    client = signed_in(backend, "alice@example.com")
    stolen = client.tokens
    assert stolen is not None
    client.logout()
    assert not client.signed_in
    replay = new_client(backend)
    replay.tokens = Tokens(access=stolen.access, refresh=stolen.refresh)
    with pytest.raises(SessionExpired):
        replay.me()


def test_errors_carry_stable_codes_and_field_details(backend: LiveBackend) -> None:
    client = new_client(backend)
    with pytest.raises(ApiError) as bad_login:
        client.login("nobody@example.com", PASSWORD)
    assert bad_login.value.code == "INVALID_CREDENTIALS" and bad_login.value.status == 401
    assert "not correct" in describe(bad_login.value).message
    assert bad_login.value.request_id

    with pytest.raises(ApiError) as weak:
        client.register("not-an-email", PASSWORD)
    shown = describe(weak.value)
    assert shown.kind == "input" and "email" in shown.fields
    with pytest.raises(ApiError) as short:
        client.register("carol@example.com", "x")
    assert "password" in describe(short.value).fields

    client.register("alice@example.com", PASSWORD)
    with pytest.raises(ApiError) as twice:
        client.register("Alice@Example.com", PASSWORD)
    assert twice.value.status == 409

    with socket.socket() as probe:  # a port nothing listens on
        probe.bind(("127.0.0.1", 0))
        closed_port = probe.getsockname()[1]
    dead = ApiClient(FrontendConfig(api_base_url=f"http://127.0.0.1:{closed_port}/api/v1"))
    with pytest.raises(ApiError) as down:
        dead.login("a@example.com", PASSWORD)
    assert down.value.code == "NETWORK_ERROR" and describe(down.value).retryable


# ------------------------------------------------------------------ documents and chat


def test_upload_progress_and_the_streamed_answer_with_citations(
    backend: LiveBackend, model: ScriptedModel
) -> None:
    client = signed_in(backend, "alice@example.com")
    document = upload_handbook(client)
    assert document["status"] == "completed" and document["chunk_count"] >= 1
    again = client.upload("handbook.pdf", handbook_pdf(), "application/pdf")
    assert again["duplicate"] and again["document"]["id"] == document["id"]  # the server decides

    session = client.create_session(title="Limits")
    events = list(client.chat_stream(session["id"], QUESTION, client.new_idempotency_key()))
    kinds = [e["type"] for e in events]
    assert kinds[0] == "start" and kinds[-1] == "complete"
    assert "token" in kinds and "status" in kinds and "citation" in kinds
    assert kinds.index("token") > kinds.index("status")
    complete = events[-1]
    assert complete["response"]["citations"] and "[1]" in complete["response"]["answer"]
    assert (
        "".join(e["text"] for e in events if e["type"] == "token")
        == (complete["response"]["answer"])
    )

    history = client.messages(session["id"])
    assert [m["role"] for m in history] == ["user", "assistant"]
    assert history[1]["id"] == complete["assistant_message_id"]  # saved before "complete"


def test_the_same_idempotency_key_replays_and_never_asks_the_model_twice(
    backend: LiveBackend, model: ScriptedModel
) -> None:
    client = signed_in(backend, "alice@example.com")
    upload_handbook(client)
    session = client.create_session()
    key = client.new_idempotency_key()
    first = list(client.chat_stream(session["id"], QUESTION, key))
    calls = len(model.calls)
    second = list(client.chat_stream(session["id"], QUESTION, key))
    assert len(model.calls) == calls  # replayed from the stored turn
    assert second[0]["replayed"] is True
    assert second[-1]["response"]["answer"] == first[-1]["response"]["answer"]
    assert len(client.messages(session["id"])) == 2  # not duplicated


def test_stream_refusals_before_it_opens_are_typed_errors(
    backend: LiveBackend, model: ScriptedModel
) -> None:
    client = signed_in(backend, "alice@example.com")
    with pytest.raises(ApiError) as missing:
        list(client.chat_stream("00000000-0000-4000-8000-000000000000", "hi", "key-12345678"))
    assert missing.value.code == "RESOURCE_NOT_FOUND"
    # a token that expired mid-conversation is refreshed before the stream opens
    session = client.create_session()
    assert client.tokens is not None
    client.tokens = Tokens(access="expired", refresh=client.tokens.refresh)
    events = list(client.chat_stream(session["id"], "hello", client.new_idempotency_key()))
    assert events[-1]["type"] == "complete"


def test_a_failing_model_ends_the_stream_with_a_typed_retryable_error(
    backend: LiveBackend, model: ScriptedModel
) -> None:
    from app.core.exceptions import ModelProviderError

    client = signed_in(backend, "alice@example.com")
    upload_handbook(client)
    session = client.create_session()
    model.script = [ModelProviderError("boom")]
    events = list(client.chat_stream(session["id"], QUESTION, client.new_idempotency_key()))
    assert events[-1]["type"] == "error" and events[-1]["code"] == "MODEL_PROVIDER_ERROR"
    assert [m["role"] for m in client.messages(session["id"])] == ["user"]  # nothing invented


# ------------------------------------------------------------------ feedback, memory, isolation


def test_feedback_and_memory_round_trips(backend: LiveBackend) -> None:
    client = signed_in(backend, "alice@example.com")
    upload_handbook(client)
    session = client.create_session()
    events = list(client.chat_stream(session["id"], QUESTION, client.new_idempotency_key()))
    message = events[-1]["assistant_message_id"]
    assert client.send_feedback(message, "up", "helpful")["rating"] == "up"
    assert db_scalar("SELECT comment FROM message_feedback") == "helpful"
    client.clear_feedback(message)
    assert db_scalar("SELECT count(*) FROM message_feedback") == 0

    assert client.preferences()["memory_enabled"] is False  # off until the user opts in
    client.update_preferences(memory_enabled=True)
    created = client.create_memory("I prefer answers in German")
    assert [m["id"] for m in client.list_memories()["items"]] == [created["id"]]
    client.update_memory(created["id"], content="I prefer answers in French")
    assert client.list_memories()["items"][0]["content"] == "I prefer answers in French"
    client.delete_memory(created["id"])
    assert client.list_memories()["items"] == []


def test_one_user_can_never_reach_another_users_data_whatever_the_client_state(
    backend: LiveBackend, model: ScriptedModel
) -> None:
    alice = signed_in(backend, "alice@example.com")
    bob = signed_in(backend, "bob@example.com")
    document = upload_handbook(alice)
    session = alice.create_session(title="private")
    events = list(alice.chat_stream(session["id"], QUESTION, alice.new_idempotency_key()))
    message = events[-1]["assistant_message_id"]
    memory = alice.create_memory("Alice prefers short answers")

    attempts = {
        "read session": lambda: bob.messages(session["id"]),
        "rename session": lambda: bob.update_session(session["id"], title="pwned"),
        "delete session": lambda: bob.delete_session(session["id"]),
        "stream into session": lambda: list(
            bob.chat_stream(session["id"], QUESTION, bob.new_idempotency_key())
        ),
        "read document": lambda: bob.get_document(document["id"]),
        "delete document": lambda: bob.delete_document(document["id"]),
        "reindex document": lambda: bob.reindex(document["id"]),
        "rate answer": lambda: bob.send_feedback(message, "down"),
        "edit memory": lambda: bob.update_memory(memory["id"], content="pwned"),
        "delete memory": lambda: bob.delete_memory(memory["id"]),
    }
    for name, attempt in attempts.items():
        with pytest.raises(ApiError) as denied:
            attempt()
        assert denied.value.status == 404, name  # the same answer as for a non-existent id
    assert bob.list_documents()["items"] == [] and bob.list_sessions()["items"] == []
    assert bob.list_memories()["items"] == []
    # nothing changed for Alice
    assert alice.list_sessions()["items"][0]["title"] == "private"
    assert alice.get_document(document["id"])["status"] == "completed"
    assert get_settings().app_env == "test"
