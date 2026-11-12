"""POST/DELETE /feedback through the real HTTP stack against PostgreSQL."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import httpx2
import pytest
from sqlalchemy import text

from app.infra.container import Infrastructure
from tests.fakes import ScriptedModel
from tests.integration.agent_helpers import (
    Actor,
    install_adapters,
    make_actor,
    make_session,
    scripted,
    seed_handbook,
)
from tests.integration.docs_helpers import API, scalar
from tests.integration.test_chat_api import post

pytestmark = pytest.mark.integration

__all__ = ["scripted"]


@pytest.fixture(autouse=True)
def _adapters() -> Iterator[None]:
    yield from install_adapters()


async def answer(
    client: httpx2.AsyncClient, infra: Infrastructure, actor: Actor
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """(session id, user message id, assistant message id) of one real answered turn."""
    await seed_handbook(infra, actor)
    session = await make_session(client, actor)
    body = (await post(client, actor, session)).json()
    return session, uuid.UUID(body["user_message_id"]), uuid.UUID(body["assistant_message_id"])


async def rate(
    client: httpx2.AsyncClient, actor: Actor, message: uuid.UUID, rating: str = "up", **extra: Any
) -> httpx2.Response:
    body = {"message_id": str(message), "rating": rating, **extra}
    return await client.post(f"{API}/feedback", json=body, headers=actor.headers)


async def count(infra: Infrastructure) -> int:
    return int(await scalar(infra, "SELECT count(*) FROM message_feedback"))


async def test_a_user_can_rate_change_and_clear_feedback_on_an_answer(
    client: httpx2.AsyncClient, infra: Infrastructure, scripted: ScriptedModel
) -> None:
    alice = await make_actor(client)
    _, _, assistant = await answer(client, infra, alice)

    first = await rate(client, alice, assistant, "up", comment="  clear and cited  ")
    assert first.status_code == 200, first.text
    assert first.json()["rating"] == "up" and first.json()["comment"] == "clear and cited"
    assert set(first.json()) == {"message_id", "rating", "comment", "updated_at"}

    changed = await rate(client, alice, assistant, "down", comment="missed the second page")
    assert changed.status_code == 200 and changed.json()["rating"] == "down"
    assert await count(infra) == 1  # updated in place, never duplicated
    assert await scalar(infra, "SELECT comment FROM message_feedback") == "missed the second page"

    blank = await rate(client, alice, assistant, "up", comment="   ")
    assert blank.json()["comment"] is None  # blank comments are not stored

    for _ in range(2):  # clearing is idempotent
        gone = await client.delete(f"{API}/feedback/{assistant}", headers=alice.headers)
        assert gone.status_code == 204
    assert await count(infra) == 0


async def test_only_the_owners_assistant_answers_can_be_rated_and_ids_cannot_be_probed(
    client: httpx2.AsyncClient, infra: Infrastructure, scripted: ScriptedModel
) -> None:
    alice = await make_actor(client, "alice@example.com")
    bob = await make_actor(client, "bob@example.com")
    session, user_message, assistant = await answer(client, infra, alice)

    answers = {
        "someone else's answer": await rate(client, bob, assistant),
        "own question (a user message)": await rate(client, alice, user_message),
        "unknown id": await rate(client, alice, uuid.uuid4()),
    }
    for name, resp in answers.items():
        assert resp.status_code == 404, name
        assert resp.json()["error"]["code"] == "RESOURCE_NOT_FOUND", name
    assert len({r.text.replace(r.json()["error"]["request_id"], "") for r in answers.values()}) == 1
    assert await count(infra) == 0

    # Bob cannot delete Alice's feedback either (the call is scoped, and silent)
    assert (await rate(client, alice, assistant)).status_code == 200
    await client.delete(f"{API}/feedback/{assistant}", headers=bob.headers)
    assert await count(infra) == 1

    # once the conversation is deleted its answers can no longer be rated
    deleted = await client.delete(f"{API}/sessions/{session}", headers=alice.headers)
    assert deleted.status_code in {200, 202, 204}
    assert (await rate(client, alice, assistant)).status_code == 404


@pytest.mark.parametrize(
    "body",
    [
        {"rating": "up"},
        {"message_id": "not-a-uuid", "rating": "up"},
        {"message_id": str(uuid.uuid4()), "rating": "meh"},
        {"message_id": str(uuid.uuid4()), "rating": "up", "comment": "x" * 1001},
        {"message_id": str(uuid.uuid4()), "rating": "up", "user_id": str(uuid.uuid4())},
    ],
)
async def test_invalid_feedback_is_rejected(
    client: httpx2.AsyncClient, body: dict[str, Any]
) -> None:
    alice = await make_actor(client)
    resp = await client.post(f"{API}/feedback", json=body, headers=alice.headers)
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_feedback_requires_authentication(client: httpx2.AsyncClient) -> None:
    body = {"message_id": str(uuid.uuid4()), "rating": "up"}
    assert (await client.post(f"{API}/feedback", json=body)).status_code == 401
    assert (await client.delete(f"{API}/feedback/{uuid.uuid4()}")).status_code == 401


async def test_feedback_is_rate_limited_per_user(
    client: httpx2.AsyncClient,
    client_with_settings: Any,
    infra: Infrastructure,
    scripted: ScriptedModel,
) -> None:
    from app.core.config import get_settings

    alice = await make_actor(client)
    _, _, assistant = await answer(client, infra, alice)
    limited = await client_with_settings(get_settings().model_copy(update={"rl_feedback_limit": 2}))
    codes = [(await rate(limited, alice, assistant)).status_code for _ in range(3)]
    assert codes == [200, 200, 429]


async def test_feedback_disappears_with_the_message_row(
    client: httpx2.AsyncClient, infra: Infrastructure, scripted: ScriptedModel
) -> None:
    alice = await make_actor(client)
    _, _, assistant = await answer(client, infra, alice)
    await rate(client, alice, assistant)
    async with infra.session_factory() as db:
        await db.execute(text("DELETE FROM messages WHERE id = :m"), {"m": assistant})
        await db.commit()
    assert await count(infra) == 0  # ON DELETE CASCADE: no orphaned feedback


async def test_my_rating_comes_back_with_the_message_list_and_only_mine(
    client: httpx2.AsyncClient, infra: Infrastructure, scripted: ScriptedModel
) -> None:
    alice = await make_actor(client)
    session, _, assistant = await answer(client, infra, alice)

    async def history() -> list[dict[str, Any]]:
        resp = await client.get(f"{API}/sessions/{session}/messages", headers=alice.headers)
        return resp.json()["items"]

    assert [m["feedback"] for m in await history()] == [None, None]
    await rate(client, alice, assistant, "down", comment="missed page 2")
    items = await history()
    assert items[0]["feedback"] is None  # the question is not an answer
    assert items[1]["feedback"] == {"rating": "down", "comment": "missed page 2"}
    await client.delete(f"{API}/feedback/{assistant}", headers=alice.headers)
    assert (await history())[1]["feedback"] is None
