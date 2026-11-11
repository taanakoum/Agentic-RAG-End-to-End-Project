"""Chat turns end to end: accept -> run the agent graph -> persist -> deliver (JSON or SSE).

Failure semantics (docs/spec/05, ADR-026):
  * Everything that can be refused is refused BEFORE a stream opens (plain JSON errors): unknown
    session, busy conversation (409), bad/reused idempotency key, rate limit, no model.
  * The pipeline runs in its own task. A lease ticker keeps the execution alive during slow
    generations; losing the lease (conversation deleted, execution reaped) cancels the pipeline.
  * A turn is COMPLETE only after the validated answer is committed to PostgreSQL. Tokens are
    streamed only after that commit, so nothing unvalidated or unsaved is ever shown. If saving
    fails the client gets `error PERSISTENCE_FAILED`, never a false success.
  * Client disconnect cancels the pipeline; the execution becomes `cancelled`, the user message is
    kept, no assistant message exists, and the conversation is free immediately.
  * Replaying the key of a completed turn streams the stored answer without any model call.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from app.agents.citations import verify_for_user
from app.agents.context import HistoryTurn
from app.agents.routing import classify
from app.agents.schemas import RAGResponse
from app.agents.single import build_single_agent_graph, run_turn
from app.agents.state import TurnContext, fresh_turn_state
from app.core.exceptions import (
    AppError,
    ConcurrencyConflictError,
    ModelNotConfiguredError,
    PersistenceError,
    ResourceNotFoundError,
    TurnFailedError,
)
from app.core.logging import get_logger
from app.infra.container import Infrastructure
from app.infra.rate_limit import subject_key
from app.repositories.conversations import ConversationRepository
from app.retrieval.models import RetrievalMode
from app.schemas.chat import AgentConfig
from app.schemas.chat_stream import (
    KEEP_ALIVE,
    AnyEvent,
    ChatResult,
    CitationEvent,
    CompleteEvent,
    ErrorEvent,
    StartEvent,
    StatusEvent,
    TokenEvent,
    sse,
    token_slices,
)
from app.services.auth_service import AuthenticatedUser
from app.services.chat_turns import ChatTurnService, TurnHandle
from app.services.factory import build_memory_service, build_retrieval_service

log = get_logger(__name__)

KEEP_ALIVE_S = 15.0
_RETRYABLE = frozenset(
    {
        "MODEL_RATE_LIMITED",
        "MODEL_TIMEOUT",
        "MODEL_PROVIDER_ERROR",
        "PERSISTENCE_FAILED",
        "SERVICE_UNAVAILABLE",
        "RETRIEVAL_TIMEOUT",
        "RATE_LIMIT_EXCEEDED",
    }
)


@dataclass(slots=True)
class TurnPlan:
    user: AuthenticatedUser
    handle: TurnHandle
    message: str
    config: AgentConfig
    history: list[HistoryTurn]


@dataclass(slots=True)
class ReplayedTurn:
    user: AuthenticatedUser
    handle: TurnHandle
    response: RAGResponse


@dataclass(slots=True)
class TurnOutcome:
    plan: TurnPlan
    response: RAGResponse
    assistant_message_id: uuid.UUID


@dataclass(slots=True)
class _Lease:
    lost: bool = False
    task: asyncio.Task[Any] | None = None
    main: asyncio.Task[Any] | None = field(default=None)


def error_event(exc: AppError) -> ErrorEvent:
    retry = exc.headers.get("Retry-After")
    return ErrorEvent(
        code=exc.code,
        message=str(exc),
        retryable=exc.code in _RETRYABLE,
        retry_after_s=int(retry) if retry and retry.isdigit() else None,
    )


class ChatService:
    def __init__(self, infra: Infrastructure) -> None:
        self._infra = infra
        self._settings = infra.settings

    # ------------------------------------------------------------------ accept

    async def prepare(
        self,
        user: AuthenticatedUser,
        session_id: uuid.UUID,
        *,
        idempotency_key: str,
        message: str,
    ) -> TurnPlan | ReplayedTurn:
        s = self._settings
        infra = self._infra
        await infra.rate_limiter.hit(
            "chat", subject_key(str(user.id)), limit=s.rl_chat_limit, window_s=s.rl_chat_window_s
        )
        if s.rl_chat_daily_limit:  # one user cannot spend the shared free-tier quota alone
            await infra.rate_limiter.hit(
                "chat_day", subject_key(str(user.id)), limit=s.rl_chat_daily_limit, window_s=86400
            )
        if classify(message).route == "kb" and not infra.llm.configured:
            raise ModelNotConfiguredError()  # refused up front: nothing is stored for it
        async with infra.session_factory() as session:
            handle = await ChatTurnService(session=session, settings=s).begin(
                user.id, session_id, idempotency_key=idempotency_key, content=message
            )
            if handle.replayed:
                return self._replay(user, handle)
            repo = ConversationRepository(session)
            conversation = await repo.get_owned(user.id, session_id)
            if conversation is None:
                raise ResourceNotFoundError()
            config = AgentConfig.model_validate(conversation.agent_config)
            rows = await repo.recent_exchanges(user.id, session_id, limit=s.chat_history_messages)
            history = [
                HistoryTurn("assistant" if m.role == "assistant" else "user", m.content)
                for m in rows
            ]
        return TurnPlan(user, handle, message.strip(), config, history)

    @staticmethod
    def _replay(user: AuthenticatedUser, handle: TurnHandle) -> ReplayedTurn:
        if handle.status == "running":
            raise ConcurrencyConflictError("This message is still being answered.")
        if handle.status != "completed" or handle.assistant_message is None:
            raise TurnFailedError()
        stored = handle.assistant_message.response
        if stored is None:
            raise TurnFailedError()
        try:
            return ReplayedTurn(user, handle, RAGResponse.model_validate(stored))
        except ValidationError as exc:  # a stored answer that no longer parses: do not invent one
            raise TurnFailedError() from exc

    # ------------------------------------------------------------------ run

    def _context(self, plan: TurnPlan, status: Any) -> TurnContext:
        infra, user, cfg = self._infra, plan.user, plan.config
        s = self._settings
        mode = RetrievalMode.HYBRID if cfg.rerank is False else None

        async def search(query: str) -> Any:
            async with infra.session_factory() as session:
                return await build_retrieval_service(session, infra).search(
                    user, query, top_k=cfg.top_k, mode=mode, rate_limited=False
                )

        async def memories(query: str) -> list[str]:
            async with infra.session_factory() as session:
                hits = await build_memory_service(session, infra).relevant(user, query)
                return [hit.memory.content for hit in hits]

        async def verify(draft: Any, evidence: Any) -> Any:
            async with infra.session_factory() as session:
                return await verify_for_user(session, user.id, draft, evidence)

        async def persist(response: dict[str, Any]) -> uuid.UUID:
            try:
                async with infra.session_factory() as session:
                    saved = await ChatTurnService(session=session, settings=s).complete(
                        user.id,
                        plan.handle.execution_id,
                        answer=str(response["answer"]),
                        response=response,
                    )
                    return saved.id
            except SQLAlchemyError as exc:
                raise PersistenceError() from exc

        return TurnContext(
            user_id=user.id,
            settings=s,
            gateway=infra.llm,
            search=search,
            memories=memories,
            verify=verify,
            persist=persist,
            history=plan.history,
            status=status,
        )

    async def _finish(self, plan: TurnPlan, error_code: str, *, cancelled: bool = False) -> None:
        """Record how a turn ended (never raises: the caller is already handling a failure)."""
        try:
            async with self._infra.session_factory() as session:
                turns = ChatTurnService(session=session, settings=self._settings)
                if cancelled:
                    await turns.cancel(
                        plan.user.id, plan.handle.execution_id, error_code=error_code
                    )
                else:
                    await turns.fail(plan.user.id, plan.handle.execution_id, error_code=error_code)
        except Exception as exc:  # the lease reaper will mark it LEASE_EXPIRED
            log.warning("turn_finish_failed", error=type(exc).__name__)

    async def _renew(self, plan: TurnPlan, lease: _Lease) -> None:
        interval = max(1.0, min(10.0, self._settings.chat_lease_s / 3))
        while True:
            await asyncio.sleep(interval)
            try:
                async with self._infra.session_factory() as session:
                    await ChatTurnService(session=session, settings=self._settings).renew_lease(
                        plan.user.id, plan.handle.execution_id
                    )
            except ConcurrencyConflictError:
                lease.lost = True  # deleted or reaped: stop working for nobody
                if lease.main is not None:
                    lease.main.cancel()
                return
            except Exception as exc:  # a failed renewal is retried on the next tick
                log.warning("lease_renewal_failed", error=type(exc).__name__)

    async def execute(self, plan: TurnPlan, *, status: Any = None) -> TurnOutcome:
        infra = self._infra
        lease = _Lease(main=asyncio.current_task())
        lease.task = asyncio.create_task(self._renew(plan, lease))
        try:
            saver = await infra.checkpointer.get()
            graph = build_single_agent_graph(
                saver, repair_budget=self._settings.llm_repair_attempts
            )
            ctx = self._context(plan, status)
            state = fresh_turn_state(plan.handle.execution_id, plan.message)
            final = await run_turn(graph, ctx, state, str(plan.handle.thread_id))
            response = RAGResponse.model_validate(final["response"])
            message_id = uuid.UUID(str(final["assistant_message_id"]))
            if ctx.cacheable and ctx.llm_response is not None:
                await infra.llm.remember(ctx.llm_response)  # only after the answer is saved
            return TurnOutcome(plan, response, message_id)
        except asyncio.CancelledError:
            if lease.lost:
                await asyncio.shield(self._finish(plan, "CONVERSATION_DELETED"))
                raise ConcurrencyConflictError(
                    "The conversation was deleted while it was answering."
                ) from None
            await asyncio.shield(self._finish(plan, "CLIENT_DISCONNECTED", cancelled=True))
            raise
        except AppError as exc:
            await asyncio.shield(self._finish(plan, exc.code))
            raise
        except Exception as exc:
            log.error("turn_failed", error=type(exc).__name__)
            await asyncio.shield(self._finish(plan, "INTERNAL_ERROR"))
            raise
        finally:
            lease.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await lease.task

    # ------------------------------------------------------------------ deliver

    async def answer(
        self,
        user: AuthenticatedUser,
        session_id: uuid.UUID,
        *,
        idempotency_key: str,
        message: str,
    ) -> ChatResult:
        planned = await self.prepare(
            user, session_id, idempotency_key=idempotency_key, message=message
        )
        if isinstance(planned, ReplayedTurn):
            handle = planned.handle
            assistant = handle.assistant_message
            return ChatResult(
                session_id=session_id,
                execution_id=handle.execution_id,
                user_message_id=handle.user_message_id,
                assistant_message_id=assistant.id if assistant else uuid.uuid4(),
                replayed=True,
                response=planned.response,
            )
        outcome = await self.execute(planned)
        return ChatResult(
            session_id=session_id,
            execution_id=planned.handle.execution_id,
            user_message_id=planned.handle.user_message_id,
            assistant_message_id=outcome.assistant_message_id,
            response=outcome.response,
        )

    @staticmethod
    def _deliver(response: RAGResponse, message_id: uuid.UUID) -> list[AnyEvent]:
        events: list[AnyEvent] = [
            TokenEvent(index=i, text=piece) for i, piece in enumerate(token_slices(response.answer))
        ]
        events += [CitationEvent(index=i, citation=c) for i, c in enumerate(response.citations)]
        events.append(CompleteEvent(assistant_message_id=message_id, response=response))
        return events

    async def stream(
        self, planned: TurnPlan | ReplayedTurn, session_id: uuid.UUID
    ) -> AsyncGenerator[bytes]:
        """SSE frames for one turn. Closing the generator (client disconnect) cancels the work."""
        handle = planned.handle
        yield sse(
            StartEvent(
                session_id=session_id,
                execution_id=handle.execution_id,
                user_message_id=handle.user_message_id,
                replayed=isinstance(planned, ReplayedTurn),
            )
        )
        if isinstance(planned, ReplayedTurn):
            message_id = handle.assistant_message.id if handle.assistant_message else uuid.uuid4()
            for event in self._deliver(planned.response, message_id):
                yield sse(event)
            return

        queue: asyncio.Queue[AnyEvent | None] = asyncio.Queue()

        async def status(stage: str) -> None:
            await queue.put(StatusEvent(stage=stage))  # pyright: ignore[reportArgumentType]

        async def pipeline() -> TurnOutcome | None:
            try:
                return await self.execute(planned, status=status)
            except AppError as exc:
                await queue.put(error_event(exc))
            except asyncio.CancelledError:
                raise
            except Exception:
                await queue.put(
                    ErrorEvent(
                        code="INTERNAL_ERROR",
                        message="Something went wrong while answering.",
                        retryable=True,
                    )
                )
            finally:
                await queue.put(None)
            return None

        task: asyncio.Task[TurnOutcome | None] = asyncio.create_task(pipeline())
        try:
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=KEEP_ALIVE_S)
                except TimeoutError:
                    yield KEEP_ALIVE
                    continue
                if item is None:
                    break
                yield sse(item)
                if isinstance(item, ErrorEvent):
                    return
            outcome = task.result()
            if outcome is None:
                return
            for event in self._deliver(outcome.response, outcome.assistant_message_id):
                yield sse(event)
        finally:
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, TimeoutError, Exception):
                    await asyncio.wait_for(asyncio.shield(task), timeout=5)
