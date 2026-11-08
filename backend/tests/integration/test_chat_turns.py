"""Same-thread concurrency, idempotency and message ordering (docs/spec/05), against PostgreSQL.

These drive ChatTurnService directly with independent database sessions, exactly like separate
API requests (or separate API processes) would.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx2
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.core.config import Settings, get_settings
from app.core.exceptions import (
    ConcurrencyConflictError,
    IdempotencyKeyReusedError,
    RequestValidationAppError,
    ResourceNotFoundError,
)
from app.infra.container import Infrastructure
from app.models.chat import ChatExecution, Message
from app.repositories.conversations import ConversationRepository
from app.services.chat_turns import ChatTurnService, TurnHandle
from tests.integration.docs_helpers import API, new_user

pytestmark = pytest.mark.integration


async def make_user_and_conversation(
    client: httpx2.AsyncClient, infra: Infrastructure, email: str = "alice@example.com"
) -> tuple[uuid.UUID, uuid.UUID]:
    headers = await new_user(client, email)
    me = await client.get(f"{API}/auth/me", headers=headers)
    session = await client.post(f"{API}/sessions", json={}, headers=headers)
    return uuid.UUID(me.json()["id"]), uuid.UUID(session.json()["id"])


async def with_service[T](
    infra: Infrastructure,
    action: Callable[[ChatTurnService], Awaitable[T]],
    *,
    settings: Settings | None = None,
    clock: Callable[[], datetime] | None = None,
) -> T:
    """Run `action` with a service on its OWN session (one session = one API request)."""
    async with infra.session_factory() as session:
        kwargs: dict[str, Any] = {"session": session, "settings": settings or get_settings()}
        if clock is not None:
            kwargs["clock"] = clock
        return await action(ChatTurnService(**kwargs))


async def begin(
    infra: Infrastructure,
    user: uuid.UUID,
    conversation: uuid.UUID,
    key: str,
    content: str = "hello there",
    **kwargs: Any,
) -> TurnHandle:
    return await with_service(
        infra,
        lambda s: s.begin(user, conversation, idempotency_key=key, content=content),
        **kwargs,
    )


async def messages_of(infra: Infrastructure, conversation: uuid.UUID) -> list[Message]:
    async with infra.session_factory() as session:
        rows = await session.execute(
            select(Message).where(Message.conversation_id == conversation).order_by(Message.seq)
        )
        return list(rows.scalars())


async def executions_of(infra: Infrastructure, conversation: uuid.UUID) -> list[ChatExecution]:
    async with infra.session_factory() as session:
        rows = await session.execute(
            select(ChatExecution).where(ChatExecution.conversation_id == conversation)
        )
        return list(rows.scalars())


# ------------------------------------------------------------------ happy path


async def test_a_turn_stores_the_question_then_the_answer_in_order(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    user, conversation = await make_user_and_conversation(client, infra)
    handle = await begin(infra, user, conversation, "key-0000001", "What is the budget?")
    assert handle.status == "running" and not handle.replayed and handle.user_message_seq == 1
    answer = await with_service(
        infra,
        lambda s: s.complete(
            user, handle.execution_id, answer="It is 5.", response={"answer": "It is 5."}
        ),
    )
    assert answer.seq == 2 and answer.role == "assistant"
    rows = await messages_of(infra, conversation)
    assert [(m.seq, m.role, m.content) for m in rows] == [
        (1, "user", "What is the budget?"),
        (2, "assistant", "It is 5."),
    ]
    assert rows[1].response == {"answer": "It is 5."}
    assert rows[0].execution_id == rows[1].execution_id == handle.execution_id
    (execution,) = await executions_of(infra, conversation)
    assert execution.status == "completed" and execution.assistant_message_id == answer.id


async def test_the_first_message_names_a_default_titled_conversation(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    user, conversation = await make_user_and_conversation(client, infra)
    await begin(infra, user, conversation, "key-0000001", "  Summarise   the   annual report ")
    async with infra.session_factory() as session:
        row = await ConversationRepository(session).get_owned(user, conversation)
        assert row is not None and row.title == "Summarise the annual report"


async def test_a_new_turn_can_start_after_the_previous_one_finished(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    user, conversation = await make_user_and_conversation(client, infra)
    for n in range(3):
        handle = await begin(infra, user, conversation, f"key-00000{n}0", f"question {n}")
        await with_service(
            infra, lambda s, h=handle: s.complete(user, h.execution_id, answer="a", response=None)
        )
    rows = await messages_of(infra, conversation)
    assert [m.seq for m in rows] == [1, 2, 3, 4, 5, 6]
    assert [m.role for m in rows] == ["user", "assistant"] * 3


# ------------------------------------------------------------------ same-thread concurrency


async def test_a_second_message_while_one_is_running_is_rejected_with_409(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    user, conversation = await make_user_and_conversation(client, infra)
    await begin(infra, user, conversation, "key-0000001")
    with pytest.raises(ConcurrencyConflictError):
        await begin(infra, user, conversation, "key-0000002", "another question")
    assert [m.seq for m in await messages_of(infra, conversation)] == [1]  # nothing was queued
    assert len(await executions_of(infra, conversation)) == 1


async def test_simultaneous_messages_in_one_conversation_exactly_one_wins(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    user, conversation = await make_user_and_conversation(client, infra)
    results = await asyncio.gather(
        *(begin(infra, user, conversation, f"race-key-{i:04d}", f"msg {i}") for i in range(12)),
        return_exceptions=True,
    )
    winners = [r for r in results if isinstance(r, TurnHandle)]
    losers = [r for r in results if isinstance(r, ConcurrencyConflictError)]
    assert len(winners) == 1 and len(losers) == 11, results
    assert [m.seq for m in await messages_of(infra, conversation)] == [1]
    running = [e for e in await executions_of(infra, conversation) if e.status == "running"]
    assert len(running) == 1 and running[0].id == winners[0].execution_id


async def test_different_conversations_of_one_user_run_concurrently(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    headers = await new_user(client)
    me = await client.get(f"{API}/auth/me", headers=headers)
    user = uuid.UUID(me.json()["id"])
    ids = [
        uuid.UUID((await client.post(f"{API}/sessions", json={}, headers=headers)).json()["id"])
        for _ in range(8)
    ]
    handles = await asyncio.gather(
        *(begin(infra, user, c, f"conc-key-{i:04d}") for i, c in enumerate(ids))
    )
    assert len({h.execution_id for h in handles}) == 8 and all(
        h.status == "running" for h in handles
    )
    # and they can all finish concurrently
    await asyncio.gather(
        *(
            with_service(
                infra, lambda s, h=h: s.complete(user, h.execution_id, answer="ok", response=None)
            )
            for h in handles
        )
    )
    for conversation in ids:
        assert [m.seq for m in await messages_of(infra, conversation)] == [1, 2]


async def test_two_users_never_block_each_other(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    a_user, a_conv = await make_user_and_conversation(client, infra, "alice@example.com")
    b_user, b_conv = await make_user_and_conversation(client, infra, "bob@example.com")
    a, b = await asyncio.gather(
        begin(infra, a_user, a_conv, "same-key-0001"), begin(infra, b_user, b_conv, "same-key-0001")
    )
    assert a.execution_id != b.execution_id  # even the very same key is per conversation


# ------------------------------------------------------------------ idempotency


async def test_replaying_a_key_returns_the_same_execution_without_new_messages(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    user, conversation = await make_user_and_conversation(client, infra)
    first = await begin(infra, user, conversation, "idem-key-001", "same question")
    again = await begin(infra, user, conversation, "idem-key-001", "same question")
    assert again.replayed and again.execution_id == first.execution_id
    assert again.user_message_id == first.user_message_id and again.status == "running"
    assert len(await messages_of(infra, conversation)) == 1
    assert len(await executions_of(infra, conversation)) == 1


async def test_replaying_a_finished_turn_returns_the_stored_answer(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    user, conversation = await make_user_and_conversation(client, infra)
    first = await begin(infra, user, conversation, "idem-key-001", "q")
    answer = await with_service(
        infra, lambda s: s.complete(user, first.execution_id, answer="the answer", response=None)
    )
    again = await begin(infra, user, conversation, "idem-key-001", "q")
    assert again.replayed and again.status == "completed"
    assert again.assistant_message is not None and again.assistant_message.id == answer.id
    assert len(await messages_of(infra, conversation)) == 2  # no duplicate of either message


async def test_replaying_a_failed_turn_reports_failure_and_does_not_rerun(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    user, conversation = await make_user_and_conversation(client, infra)
    first = await begin(infra, user, conversation, "idem-key-001", "q")
    assert await with_service(
        infra, lambda s: s.fail(user, first.execution_id, error_code="MODEL_ERROR")
    )
    again = await begin(infra, user, conversation, "idem-key-001", "q")
    assert again.replayed and again.status == "failed" and again.assistant_message is None
    assert len(await executions_of(infra, conversation)) == 1  # a retry needs a NEW key


async def test_reusing_a_key_with_a_different_message_is_an_error(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    user, conversation = await make_user_and_conversation(client, infra)
    await begin(infra, user, conversation, "idem-key-001", "original")
    with pytest.raises(IdempotencyKeyReusedError):
        await begin(infra, user, conversation, "idem-key-001", "something else")


async def test_many_simultaneous_retries_of_one_request_create_one_execution(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    user, conversation = await make_user_and_conversation(client, infra)
    handles = await asyncio.gather(
        *(begin(infra, user, conversation, "retry-storm-01", "q") for _ in range(10))
    )
    assert len({h.execution_id for h in handles}) == 1
    assert sum(1 for h in handles if not h.replayed) == 1  # exactly one real start
    assert len(await messages_of(infra, conversation)) == 1
    assert len(await executions_of(infra, conversation)) == 1


# ------------------------------------------------------------------ leases and stale writers


async def test_an_abandoned_execution_is_reaped_after_its_lease_and_cannot_write_late(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    user, conversation = await make_user_and_conversation(client, infra)
    t0 = datetime.now(UTC)
    old = await begin(infra, user, conversation, "dead-worker-1", "q1", clock=lambda: t0)
    lease = get_settings().chat_lease_s
    # still inside the lease: rejected
    with pytest.raises(ConcurrencyConflictError):
        await begin(
            infra,
            user,
            conversation,
            "new-request-1",
            "q2",
            clock=lambda: t0 + timedelta(seconds=lease - 1),
        )
    # after the lease: the dead execution is failed and the new request proceeds
    fresh = await begin(
        infra,
        user,
        conversation,
        "new-request-1",
        "q2",
        clock=lambda: t0 + timedelta(seconds=lease + 1),
    )
    assert fresh.status == "running" and fresh.user_message_seq == 2
    states = {e.id: (e.status, e.error_code) for e in await executions_of(infra, conversation)}
    assert states[old.execution_id] == ("failed", "LEASE_EXPIRED")
    # the old worker wakes up and tries to save its answer: refused, nothing is overwritten
    with pytest.raises(ConcurrencyConflictError):
        await with_service(
            infra, lambda s: s.complete(user, old.execution_id, answer="late", response=None)
        )
    assert [m.role for m in await messages_of(infra, conversation)] == ["user", "user"]


async def test_renewing_the_lease_keeps_a_long_generation_alive(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    user, conversation = await make_user_and_conversation(client, infra)
    t0 = datetime.now(UTC)
    lease = get_settings().chat_lease_s
    handle = await begin(infra, user, conversation, "long-run-0001", "q", clock=lambda: t0)
    later = t0 + timedelta(seconds=lease - 10)
    await with_service(
        infra, lambda s: s.renew_lease(user, handle.execution_id), clock=lambda: later
    )
    with pytest.raises(ConcurrencyConflictError):  # old lease would have ended; renewed one has not
        await begin(
            infra,
            user,
            conversation,
            "other-request-1",
            "q2",
            clock=lambda: t0 + timedelta(seconds=lease + 5),
        )


async def test_a_turn_cannot_be_completed_twice_or_after_failing(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    user, conversation = await make_user_and_conversation(client, infra)
    handle = await begin(infra, user, conversation, "key-0000001")
    await with_service(
        infra, lambda s: s.complete(user, handle.execution_id, answer="a", response=None)
    )
    with pytest.raises(ConcurrencyConflictError):
        await with_service(
            infra,
            lambda s: s.complete(user, handle.execution_id, answer="again", response=None),
        )
    with pytest.raises(ConcurrencyConflictError):
        await with_service(infra, lambda s: s.renew_lease(user, handle.execution_id))
    assert not await with_service(
        infra, lambda s: s.fail(user, handle.execution_id, error_code="X")
    )  # already final: nothing changes
    assert [m.role for m in await messages_of(infra, conversation)] == ["user", "assistant"]


async def test_a_failed_turn_frees_the_conversation_for_the_next_message(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    user, conversation = await make_user_and_conversation(client, infra)
    first = await begin(infra, user, conversation, "key-0000001")
    await with_service(infra, lambda s: s.fail(user, first.execution_id, error_code="TIMEOUT"))
    second = await begin(infra, user, conversation, "key-0000002", "retry with a new key")
    assert second.status == "running" and second.user_message_seq == 2


async def test_two_simultaneous_completions_of_one_execution_store_one_answer(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    user, conversation = await make_user_and_conversation(client, infra)
    handle = await begin(infra, user, conversation, "key-0000001")
    results = await asyncio.gather(
        *(
            with_service(
                infra,
                lambda s, i=i: s.complete(user, handle.execution_id, answer=f"a{i}", response=None),
            )
            for i in range(6)
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(r, Message) for r in results) == 1
    assert all(isinstance(r, (Message, ConcurrencyConflictError)) for r in results)
    assert [m.role for m in await messages_of(infra, conversation)] == ["user", "assistant"]


# ------------------------------------------------------------------ deletion during generation


async def test_deleting_a_conversation_cancels_the_running_turn_and_refuses_its_answer(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    headers = await new_user(client)
    me = await client.get(f"{API}/auth/me", headers=headers)
    user = uuid.UUID(me.json()["id"])
    conversation = uuid.UUID(
        (await client.post(f"{API}/sessions", json={}, headers=headers)).json()["id"]
    )
    handle = await begin(infra, user, conversation, "key-0000001")
    assert (
        await client.delete(f"{API}/sessions/{conversation}", headers=headers)
    ).status_code == 202
    (execution,) = await executions_of(infra, conversation)
    assert execution.status == "cancelled" and execution.error_code == "CONVERSATION_DELETED"
    with pytest.raises(ConcurrencyConflictError):
        await with_service(
            infra, lambda s: s.complete(user, handle.execution_id, answer="late", response=None)
        )
    assert [m.role for m in await messages_of(infra, conversation)] == ["user"]
    with pytest.raises(ResourceNotFoundError):  # and no new turn can start on a deleted one
        await begin(infra, user, conversation, "key-0000002")


# ------------------------------------------------------------------ ownership and validation


async def test_another_user_cannot_start_a_turn_or_finish_someone_elses(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    a_user, a_conv = await make_user_and_conversation(client, infra, "alice@example.com")
    b_user, _ = await make_user_and_conversation(client, infra, "bob@example.com")
    with pytest.raises(ResourceNotFoundError):
        await begin(infra, b_user, a_conv, "key-0000001")
    handle = await begin(infra, a_user, a_conv, "key-0000001")
    with pytest.raises(ResourceNotFoundError):
        await with_service(
            infra, lambda s: s.complete(b_user, handle.execution_id, answer="x", response=None)
        )
    assert not await with_service(
        infra, lambda s: s.fail(b_user, handle.execution_id, error_code="X")
    )
    with pytest.raises(ConcurrencyConflictError):
        await with_service(infra, lambda s: s.renew_lease(b_user, handle.execution_id))
    (execution,) = await executions_of(infra, a_conv)
    assert execution.status == "running"  # untouched by the other user's attempts


@pytest.mark.parametrize(
    ("key", "content"),
    [
        ("short", "hello"),
        ("has space in it", "hello"),
        ("ok-key-000001", ""),
        ("ok-key-000001", "   \n\t "),
        ("ok-key-000001", "x" * 8001),
        ("k" * 129, "hello"),
        ("emoji-key-😀-0001", "hello"),
    ],
)
async def test_invalid_keys_and_messages_are_rejected_before_anything_is_stored(
    client: httpx2.AsyncClient, infra: Infrastructure, key: str, content: str
) -> None:
    user, conversation = await make_user_and_conversation(client, infra)
    with pytest.raises(RequestValidationAppError):
        await begin(infra, user, conversation, key, content)
    assert await messages_of(infra, conversation) == []
    assert await executions_of(infra, conversation) == []


# ------------------------------------------------------------------ database safety nets


async def test_the_database_itself_rejects_duplicate_sequences_and_two_running_executions(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    user, conversation = await make_user_and_conversation(client, infra)
    await begin(infra, user, conversation, "key-0000001")
    async with infra.session_factory() as session:
        session.add(
            Message(conversation_id=conversation, user_id=user, seq=1, role="user", content="dup")
        )
        with pytest.raises(IntegrityError):
            await session.commit()
    async with infra.session_factory() as session:
        for key in ("raw-key-00001", "raw-key-00002"):
            session.add(
                ChatExecution(
                    conversation_id=conversation,
                    user_id=user,
                    idempotency_key=key,
                    request_hash="0" * 64,
                    status="running",
                    user_message_id=uuid.uuid4(),
                    lease_expires_at=datetime.now(UTC),
                )
            )
        with pytest.raises(IntegrityError):  # a second running execution is impossible
            await session.commit()
    async with infra.session_factory() as session:
        session.add(
            ChatExecution(
                conversation_id=conversation,
                user_id=user,
                idempotency_key="key-0000001",
                request_hash="0" * 64,
                status="failed",
                user_message_id=uuid.uuid4(),
                lease_expires_at=datetime.now(UTC),
            )
        )
        with pytest.raises(IntegrityError):  # the idempotency key is unique per conversation
            await session.commit()


async def test_message_sequences_stay_gapless_and_ordered_under_heavy_parallel_use(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    user, conversation = await make_user_and_conversation(client, infra)
    done = 0
    for round_ in range(6):
        # a storm of attempts; whoever wins finishes; losers must not leave a trace
        attempts = await asyncio.gather(
            *(begin(infra, user, conversation, f"storm-{round_}-{i:03d}") for i in range(6)),
            return_exceptions=True,
        )
        winner = next(a for a in attempts if isinstance(a, TurnHandle))
        await with_service(
            infra, lambda s, h=winner: s.complete(user, h.execution_id, answer="a", response=None)
        )
        done += 1
    rows = await messages_of(infra, conversation)
    assert [m.seq for m in rows] == list(range(1, 2 * done + 1))
    async with infra.session_factory() as session:
        last_seq = (
            await session.execute(
                text("SELECT last_seq FROM conversations WHERE id = :id"), {"id": conversation}
            )
        ).scalar_one()
    assert last_seq == 2 * done


async def test_message_reads_are_scoped_to_the_owner_at_the_repository_level(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    a_user, a_conv = await make_user_and_conversation(client, infra, "alice@example.com")
    b_user, _ = await make_user_and_conversation(client, infra, "bob@example.com")
    handle = await begin(infra, a_user, a_conv, "key-0000001", "private question")
    await with_service(
        infra, lambda s: s.complete(a_user, handle.execution_id, answer="a", response=None)
    )
    async with infra.session_factory() as session:
        repo = ConversationRepository(session)
        mine, _ = await repo.list_messages(a_user, a_conv, after_seq=0, limit=10)
        theirs, _ = await repo.list_messages(b_user, a_conv, after_seq=0, limit=10)
        assert [m.seq for m in mine] == [1, 2] and theirs == []
        assert await repo.recent_messages(b_user, a_conv, limit=10) == []
        assert [m.seq for m in await repo.recent_messages(a_user, a_conv, limit=10)] == [1, 2]
