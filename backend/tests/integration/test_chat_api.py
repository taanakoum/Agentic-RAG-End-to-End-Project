"""POST /chat and POST /chat/stream through the real HTTP stack (ASGI), plus real-socket tests."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncGenerator, Iterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import httpx2
import pytest
import uvicorn
from sqlalchemy import text

from app.core.config import get_settings
from app.core.exceptions import ModelProviderError, ModelRateLimitedError
from app.infra.container import Infrastructure
from app.llm.cache import LLMGateway
from app.schemas.chat_stream import parse_sse
from app.services import chat_service
from tests.fakes import ScriptedModel
from tests.integration.agent_helpers import (
    Actor,
    answer_json,
    install_adapters,
    make_actor,
    make_session,
    scripted,
    seed_handbook,
)
from tests.integration.conftest import build_app
from tests.integration.docs_helpers import API, bearer, scalar

pytestmark = pytest.mark.integration

__all__ = ["scripted"]

QUESTION = "What is the maximum upload size?"


@pytest.fixture(autouse=True)
def _adapters() -> Iterator[None]:
    yield from install_adapters()


_KEYS = iter(range(1, 10_000_000))


def key() -> str:
    return f"api-key-{next(_KEYS):09d}"


async def post(
    client: httpx2.AsyncClient,
    actor: Actor,
    session: uuid.UUID,
    message: str = QUESTION,
    *,
    path: str = "/chat",
    idempotency_key: str | None = None,
) -> httpx2.Response:
    headers = {**actor.headers, "Idempotency-Key": idempotency_key or key()}
    return await client.post(
        f"{API}{path}", json={"session_id": str(session), "message": message}, headers=headers
    )


async def stream_frames(
    client: httpx2.AsyncClient,
    actor: Actor,
    session: uuid.UUID,
    message: str = QUESTION,
    *,
    idempotency_key: str | None = None,
) -> list[dict[str, object]]:
    response = await post(
        client, actor, session, message, path="/chat/stream", idempotency_key=idempotency_key
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"
    frames = [f for f in response.content.split(b"\n\n") if f.strip() and not f.startswith(b":")]
    return [parse_sse(f + b"\n\n") for f in frames]


def types(events: list[dict[str, object]]) -> list[str]:
    return [str(e["type"]) for e in events]


# ------------------------------------------------------------------ non-streaming


async def test_chat_returns_one_validated_cited_answer(
    client: httpx2.AsyncClient, infra: Infrastructure, scripted: ScriptedModel
) -> None:
    alice = await make_actor(client)
    await seed_handbook(infra, alice)
    session = await make_session(client, alice)
    resp = await post(client, alice, session)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {
        "session_id",
        "execution_id",
        "user_message_id",
        "assistant_message_id",
        "replayed",
        "response",
    }
    r = body["response"]
    assert r["schema_version"] == "1.0" and r["answer"].endswith("[1].")
    assert set(r["citations"][0]) == {
        "document_id",
        "document_name",
        "page_number",
        "chunk_id",
        "excerpt",
    }
    assert r["metadata"]["route"] == "kb" and r["insufficient_evidence"] is False
    # reopened later through the history endpoint
    messages = await client.get(f"{API}/sessions/{session}/messages", headers=alice.headers)
    items = messages.json()["items"]
    assert [m["role"] for m in items] == ["user", "assistant"]
    assert items[1]["response"] == r and items[1]["id"] == body["assistant_message_id"]


async def test_the_idempotency_key_is_required_and_validated(
    client: httpx2.AsyncClient, infra: Infrastructure, scripted: ScriptedModel
) -> None:
    alice = await make_actor(client)
    session = await make_session(client, alice)
    body = {"session_id": str(session), "message": "hello"}
    missing = await client.post(f"{API}/chat", json=body, headers=alice.headers)
    assert missing.status_code == 422 and missing.json()["error"]["code"] == "VALIDATION_ERROR"
    for bad in ("short", "has spaces in it!", "x" * 129):
        resp = await client.post(
            f"{API}/chat", json=body, headers={**alice.headers, "Idempotency-Key": bad}
        )
        assert resp.status_code == 422, bad
    assert scripted.calls == []


@pytest.mark.parametrize(
    "body",
    [
        {"message": "hi"},
        {"session_id": "not-a-uuid", "message": "hi"},
        {"session_id": str(uuid.uuid4()), "message": ""},
        {"session_id": str(uuid.uuid4()), "message": "hi", "model": "gpt-4"},
        {"session_id": str(uuid.uuid4()), "message": "x" * 100_001},
    ],
)
async def test_invalid_chat_requests_are_rejected(
    client: httpx2.AsyncClient, body: dict[str, Any]
) -> None:
    alice = await make_actor(client)
    resp = await client.post(
        f"{API}/chat", json=body, headers={**alice.headers, "Idempotency-Key": key()}
    )
    assert resp.status_code in (404, 422), resp.text
    assert "request_id" in resp.json()["error"]


async def test_chat_requires_authentication_and_a_verified_email_when_configured(
    client: httpx2.AsyncClient, client_with_settings: Any
) -> None:
    body = {"session_id": str(uuid.uuid4()), "message": "hi"}
    for headers in ({}, bearer("garbage")):
        for path in ("/chat", "/chat/stream"):
            resp = await client.post(
                f"{API}{path}", json=body, headers={**headers, "Idempotency-Key": key()}
            )
            assert resp.status_code == 401
    strict: httpx2.AsyncClient = await client_with_settings(
        get_settings().model_copy(update={"require_verified_email": True})
    )
    alice = await make_actor(strict)
    resp = await strict.post(
        f"{API}/chat", json=body, headers={**alice.headers, "Idempotency-Key": key()}
    )
    assert resp.status_code == 403 and resp.json()["error"]["code"] == "EMAIL_NOT_VERIFIED"


async def test_another_users_session_is_a_404_identical_to_an_unknown_one(
    client: httpx2.AsyncClient, infra: Infrastructure, scripted: ScriptedModel
) -> None:
    alice = await make_actor(client, "alice@example.com")
    bob = await make_actor(client, "bob@example.com")
    session = await make_session(client, alice)
    foreign = await post(client, bob, session)
    unknown = await post(client, bob, uuid.uuid4())
    assert foreign.status_code == unknown.status_code == 404
    assert foreign.json()["error"]["code"] == unknown.json()["error"]["code"]
    assert scripted.calls == []
    assert await scalar(infra, "SELECT count(*) FROM messages") == 0


async def test_chat_is_rate_limited_per_user(
    client_with_settings: Any,
) -> None:
    settings = get_settings().model_copy(update={"rl_chat_limit": 3})
    limited: httpx2.AsyncClient = await client_with_settings(settings)
    alice = await make_actor(limited)
    session = await make_session(limited, alice)
    codes = [(await post(limited, alice, session, "hello")).status_code for _ in range(5)]
    assert codes == [200, 200, 200, 429, 429]
    bob = await make_actor(limited, "bob@example.com")
    assert (await post(limited, bob, await make_session(limited, bob), "hello")).status_code == 200


async def test_provider_failures_map_to_stable_http_errors(
    client: httpx2.AsyncClient, infra: Infrastructure, scripted: ScriptedModel
) -> None:
    alice = await make_actor(client)
    await seed_handbook(infra, alice)
    session = await make_session(client, alice)
    scripted.script = [ModelRateLimitedError(17)]
    limited = await post(client, alice, session)
    assert limited.status_code == 429 and limited.headers["retry-after"] == "17"
    assert limited.json()["error"]["code"] == "MODEL_RATE_LIMITED"
    scripted.script = [ModelProviderError()]
    broken = await post(client, alice, session)
    assert broken.status_code == 502 and broken.json()["error"]["code"] == "MODEL_PROVIDER_ERROR"
    assert "Traceback" not in broken.text and "scripted" not in broken.text


async def test_a_busy_conversation_answers_409_and_the_first_request_still_succeeds(
    client: httpx2.AsyncClient, infra: Infrastructure, scripted: ScriptedModel
) -> None:
    alice = await make_actor(client)
    await seed_handbook(infra, alice)
    session = await make_session(client, alice)
    scripted.delay_s = 0.4
    first, second = await asyncio.gather(
        post(client, alice, session), post(client, alice, session, "another question")
    )
    assert sorted([first.status_code, second.status_code]) == [200, 409]
    loser = first if first.status_code == 409 else second
    assert loser.json()["error"]["code"] == "CONCURRENCY_CONFLICT"


async def test_replay_with_the_same_key_is_idempotent_and_a_changed_body_is_refused(
    client: httpx2.AsyncClient, infra: Infrastructure, scripted: ScriptedModel
) -> None:
    alice = await make_actor(client)
    await seed_handbook(infra, alice)
    session = await make_session(client, alice)
    first = await post(client, alice, session, idempotency_key="idem-key-0001")
    again = await post(client, alice, session, idempotency_key="idem-key-0001")
    assert again.status_code == 200 and again.json()["replayed"] is True
    assert again.json()["response"] == first.json()["response"] and len(scripted.calls) == 1
    changed = await post(
        client, alice, session, "a different question", idempotency_key="idem-key-0001"
    )
    assert (
        changed.status_code == 409 and changed.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"
    )
    assert await scalar(infra, "SELECT count(*) FROM messages") == 2  # one turn, never duplicated


async def test_ten_simultaneous_retries_of_one_request_create_one_answer(
    client: httpx2.AsyncClient, infra: Infrastructure, scripted: ScriptedModel
) -> None:
    alice = await make_actor(client)
    await seed_handbook(infra, alice)
    session = await make_session(client, alice)
    scripted.delay_s = 0.2
    results = await asyncio.gather(
        *(post(client, alice, session, idempotency_key="storm-key-0001") for _ in range(10))
    )
    codes = sorted(r.status_code for r in results)
    assert codes.count(200) >= 1 and set(codes) <= {200, 409}  # 409 = "still being answered"
    assert len(scripted.calls) == 1 and await scalar(infra, "SELECT count(*) FROM messages") == 2
    final = await post(client, alice, session, idempotency_key="storm-key-0001")
    assert final.status_code == 200 and final.json()["replayed"] is True


async def test_a_failed_turn_cannot_be_replayed(
    client: httpx2.AsyncClient, infra: Infrastructure, scripted: ScriptedModel
) -> None:
    alice = await make_actor(client)
    await seed_handbook(infra, alice)
    session = await make_session(client, alice)
    scripted.script = [ModelProviderError()]
    assert (await post(client, alice, session, idempotency_key="fail-key-0001")).status_code == 502
    again = await post(client, alice, session, idempotency_key="fail-key-0001")
    assert again.status_code == 409 and again.json()["error"]["code"] == "TURN_FAILED"


async def test_without_a_configured_model_document_questions_are_a_clear_503(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    alice = await make_actor(client)
    session = await make_session(client, alice)
    resp = await post(client, alice, session)  # the real, unconfigured provider
    assert resp.status_code == 503 and resp.json()["error"]["code"] == "MODEL_NOT_CONFIGURED"
    assert (await post(client, alice, session, "hello")).status_code == 200  # no model needed


# ------------------------------------------------------------------ streaming


async def test_a_streamed_turn_has_the_documented_event_order_and_one_terminal_event(
    client: httpx2.AsyncClient, infra: Infrastructure, scripted: ScriptedModel
) -> None:
    alice = await make_actor(client)
    await seed_handbook(infra, alice)
    session = await make_session(client, alice)
    events = await stream_frames(client, alice, session)
    kinds = types(events)
    assert kinds[0] == "start" and kinds[-1] == "complete" and kinds.count("complete") == 1
    assert "error" not in kinds
    body = kinds[1:-1]
    # status*, token+, citation*
    boundary = next(i for i, k in enumerate(body) if k != "status")
    assert set(body[:boundary]) == {"status"} and body[boundary] == "token"
    tail = body[boundary:]
    assert tail == sorted(
        tail, key=lambda k: ["token", "citation"].index(k)
    )  # tokens, then citations
    stages = [e["stage"] for e in events if e["type"] == "status"]
    assert stages == ["retrieving", "generating", "verifying", "saving"]
    start, complete = events[0], events[-1]
    response = complete["response"]
    assert isinstance(response, dict)
    text_ = "".join(str(e["text"]) for e in events if e["type"] == "token")
    assert text_ == response["answer"]  # slices concatenate to exactly the validated answer
    assert [e["index"] for e in events if e["type"] == "token"] == list(range(kinds.count("token")))
    cited = [e["citation"] for e in events if e["type"] == "citation"]
    assert cited == response["citations"] and start["replayed"] is False
    # the turn is persisted by the time the first token is sent
    rows = await client.get(f"{API}/sessions/{session}/messages", headers=alice.headers)
    assert rows.json()["items"][1]["id"] == complete["assistant_message_id"]


async def test_a_streamed_and_a_plain_turn_give_the_same_answer(
    client: httpx2.AsyncClient, infra: Infrastructure, scripted: ScriptedModel
) -> None:
    alice = await make_actor(client)
    await seed_handbook(infra, alice)
    plain = (await post(client, alice, await make_session(client, alice))).json()["response"]
    last = (await stream_frames(client, alice, await make_session(client, alice)))[-1]
    streamed: dict[str, Any] = json.loads(json.dumps(last["response"]))
    plain_meta: dict[str, Any] = plain["metadata"]
    streamed_meta: dict[str, Any] = streamed["metadata"]
    plain_meta.pop("cache_status")  # miss vs hit
    streamed_meta.pop("cache_status")
    assert plain == streamed


async def test_smalltalk_streams_without_status_stages_that_need_a_model(
    client: httpx2.AsyncClient, infra: Infrastructure, scripted: ScriptedModel
) -> None:
    alice = await make_actor(client)
    session = await make_session(client, alice)
    events = await stream_frames(client, alice, session, "hello")
    assert types(events)[0] == "start" and types(events)[-1] == "complete"
    assert [e["stage"] for e in events if e["type"] == "status"] == ["saving"]
    assert scripted.calls == []


async def test_a_provider_error_after_the_stream_opened_is_one_error_event_and_nothing_else(
    client: httpx2.AsyncClient, infra: Infrastructure, scripted: ScriptedModel
) -> None:
    alice = await make_actor(client)
    await seed_handbook(infra, alice)
    session = await make_session(client, alice)
    scripted.script = [ModelRateLimitedError(23)]
    events = await stream_frames(client, alice, session)
    kinds = types(events)
    assert kinds[0] == "start" and kinds[-1] == "error" and kinds.count("error") == 1
    assert "token" not in kinds and "complete" not in kinds
    error = events[-1]
    assert error["code"] == "MODEL_RATE_LIMITED" and error["retryable"] is True
    assert error["retry_after_s"] == 23
    assert await scalar(infra, "SELECT count(*) FROM messages WHERE role = 'assistant'") == 0
    scripted.script = [ModelProviderError()]
    again = await stream_frames(client, alice, session)
    assert again[-1]["code"] == "MODEL_PROVIDER_ERROR" and again[-1]["retryable"] is True


async def test_refusals_before_the_stream_opens_are_plain_json_errors(
    client: httpx2.AsyncClient, infra: Infrastructure, scripted: ScriptedModel
) -> None:
    alice = await make_actor(client)
    bob = await make_actor(client, "bob@example.com")
    await seed_handbook(infra, alice)
    session = await make_session(client, alice)
    resp = await post(client, bob, session, path="/chat/stream")
    assert resp.status_code == 404 and resp.headers["content-type"].startswith("application/json")
    missing_key = await client.post(
        f"{API}/chat/stream",
        json={"session_id": str(session), "message": "hi"},
        headers=alice.headers,
    )
    assert missing_key.status_code == 422
    scripted.delay_s = 0.4
    first, second = await asyncio.gather(
        post(client, alice, session, path="/chat/stream"),
        post(client, alice, session, "second", path="/chat/stream"),
    )
    codes = sorted([first.status_code, second.status_code])
    assert codes == [200, 409]
    loser = first if first.status_code == 409 else second
    assert loser.headers["content-type"].startswith("application/json")


async def test_replaying_a_streamed_turn_replays_the_stored_answer_without_the_model(
    client: httpx2.AsyncClient, infra: Infrastructure, scripted: ScriptedModel
) -> None:
    alice = await make_actor(client)
    await seed_handbook(infra, alice)
    session = await make_session(client, alice)
    first = await stream_frames(client, alice, session, idempotency_key="replay-key-0001")
    again = await stream_frames(client, alice, session, idempotency_key="replay-key-0001")
    assert again[0]["replayed"] is True and types(again)[-1] == "complete"
    assert "status" not in types(again)  # nothing is re-run
    assert again[-1]["response"] == first[-1]["response"]
    assert again[-1]["assistant_message_id"] == first[-1]["assistant_message_id"]
    assert len(scripted.calls) == 1


async def test_a_persistence_failure_streams_an_error_and_never_a_false_success(
    client: httpx2.AsyncClient,
    infra: Infrastructure,
    scripted: ScriptedModel,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sqlalchemy.exc import OperationalError

    from app.services.chat_turns import ChatTurnService

    async def broken(self: ChatTurnService, *args: Any, **kwargs: Any) -> Any:
        raise OperationalError("INSERT", {}, Exception("disk full"))

    alice = await make_actor(client)
    await seed_handbook(infra, alice)
    session = await make_session(client, alice)
    monkeypatch.setattr(ChatTurnService, "complete", broken)
    events = await stream_frames(client, alice, session)
    kinds = types(events)
    assert kinds[-1] == "error" and events[-1]["code"] == "PERSISTENCE_FAILED"
    assert (
        "token" not in kinds and "complete" not in kinds
    )  # no text is shown for an unsaved answer
    assert events[-1]["retryable"] is True


async def test_keep_alive_comments_keep_a_slow_generation_connection_open(
    client: httpx2.AsyncClient,
    infra: Infrastructure,
    scripted: ScriptedModel,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(chat_service, "KEEP_ALIVE_S", 0.1)
    alice = await make_actor(client)
    await seed_handbook(infra, alice)
    session = await make_session(client, alice)
    scripted.delay_s = 0.5
    response = await post(client, alice, session, path="/chat/stream")
    assert response.content.count(b": keep-alive") >= 2
    assert response.content.rstrip().endswith(b"}")  # and the stream still ends with `complete`


# ------------------------------------------------------------------ disconnects and leases


async def drive_until_status(infra: Infrastructure, actor: Actor, session: uuid.UUID) -> Any:
    """Open the stream at service level and stop reading after the first `status` event."""
    service = chat_service.ChatService(infra)
    planned = await service.prepare(actor.user, session, idempotency_key=key(), message=QUESTION)
    generator = service.stream(planned, session)
    frames: list[dict[str, object]] = []
    async for frame in generator:
        frames.append(parse_sse(frame))
        if frames[-1]["type"] == "status":
            break
    return generator, planned, frames


async def test_closing_the_stream_cancels_the_turn_and_frees_the_conversation(
    client: httpx2.AsyncClient, infra: Infrastructure, scripted: ScriptedModel
) -> None:
    alice = await make_actor(client)
    await seed_handbook(infra, alice)
    session = await make_session(client, alice)
    scripted.delay_s = 3.0
    generator, planned, frames = await drive_until_status(infra, alice, session)
    assert [f["type"] for f in frames] == ["start", "status"]
    await generator.aclose()  # what Starlette does when the client disconnects
    row = (
        await (await infra.session_factory().__aenter__()).execute(
            text("SELECT status, error_code FROM chat_executions WHERE id = :i"),
            {"i": planned.handle.execution_id},  # type: ignore[union-attr]
        )
    ).one()
    assert (row.status, row.error_code) == ("cancelled", "CLIENT_DISCONNECTED")
    assert await scalar(infra, "SELECT count(*) FROM messages WHERE role = 'assistant'") == 0
    assert await scalar(infra, "SELECT count(*) FROM messages WHERE role = 'user'") == 1  # kept
    scripted.delay_s = 0.0
    follow = await post(
        client, alice, session, "next question"
    )  # not blocked by the cancelled turn
    assert follow.status_code == 200


@asynccontextmanager
async def live_server(infra: Infrastructure) -> AsyncGenerator[str]:
    """The real app on a real socket (so a real client disconnect can be tested)."""
    app = build_app(get_settings(), infra)
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="off")
    )
    task = asyncio.create_task(server.serve())
    for _ in range(200):
        if server.started:
            break
        await asyncio.sleep(0.05)
    assert server.started
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=10)


async def test_a_real_client_disconnect_cancels_the_generation(
    client: httpx2.AsyncClient, infra: Infrastructure, scripted: ScriptedModel
) -> None:
    alice = await make_actor(client)
    await seed_handbook(infra, alice)
    session = await make_session(client, alice)
    scripted.delay_s = 4.0
    async with live_server(infra) as base, httpx.AsyncClient(base_url=base, timeout=30) as real:
        async with real.stream(
            "POST",
            f"{API}/chat/stream",
            json={"session_id": str(session), "message": QUESTION},
            headers={**alice.headers, "Idempotency-Key": key()},
        ) as response:
            assert response.status_code == 200
            seen = ""
            async for chunk in response.aiter_text():
                seen += chunk
                if "event: status" in seen:
                    break
        # leaving the block closes the TCP connection mid-generation
        status = ""
        for _ in range(100):
            status = str(
                await scalar(
                    infra, "SELECT status FROM chat_executions ORDER BY created_at DESC LIMIT 1"
                )
            )
            if status != "running":
                break
            await asyncio.sleep(0.1)
    assert status == "cancelled"
    assert await scalar(infra, "SELECT count(*) FROM messages WHERE role = 'assistant'") == 0


async def test_the_lease_is_renewed_while_a_slow_generation_runs(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    settings = get_settings().model_copy(update={"chat_lease_s": 5})
    own = Infrastructure.create(settings)
    try:
        model = ScriptedModel(
            answer_json(
                "Files larger than 25 megabytes are rejected [S1].",
                ("S1", "larger than 25 megabytes are rejected"),
            )
        )
        model.delay_s = 8.0  # longer than the 5 s lease
        own.llm = LLMGateway(model, own.redis, settings)
        alice = await make_actor(client)
        await seed_handbook(infra, alice)
        session = await make_session(client, alice)
        service = chat_service.ChatService(own)
        running = asyncio.create_task(
            service.answer(alice.user, session, idempotency_key=key(), message=QUESTION)
        )
        await asyncio.sleep(6.5)  # the original lease (5 s) has run out by now
        with pytest.raises(
            Exception
        ) as busy:  # a second request must still see the turn as running
            await service.answer(alice.user, session, idempotency_key=key(), message="another one")
        assert getattr(busy.value, "code", "") == "CONCURRENCY_CONFLICT"
        result = await running
        assert result.response.citations  # the slow answer completed and was saved
    finally:
        await own.close()
