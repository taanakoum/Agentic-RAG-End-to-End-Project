"""Work that runs after the HTTP response (own DB session, never raises into the server)."""

from __future__ import annotations

import uuid

from app.core.logging import get_logger
from app.infra.container import Infrastructure
from app.infra.email import OutgoingEmail
from app.services.factory import build_account_service

log = get_logger(__name__)


async def send_password_reset_email(infra: Infrastructure, email: str) -> None:
    try:
        async with infra.session_factory() as session:
            await build_account_service(session, infra).send_password_reset(email)
    except Exception:
        log.exception("background_password_reset_failed")


async def send_verification_email(infra: Infrastructure, user_id: uuid.UUID) -> None:
    try:
        async with infra.session_factory() as session:
            await build_account_service(session, infra).send_verification(user_id)
    except Exception:
        log.exception("background_verification_failed")


async def kick_dispatcher(infra: Infrastructure) -> None:
    """Publish pending outbox events right away (best effort; Beat guarantees delivery anyway)."""
    import asyncio

    from app.ingestion.outbox import dispatch_outbox
    from app.tasks.transport import task_sender

    try:
        await asyncio.to_thread(
            dispatch_outbox,
            infra.sync_session_factory,
            task_sender(infra.settings),
            batch_size=infra.settings.outbox_batch_size,
        )
    except Exception:
        log.warning("dispatcher_kick_failed")


async def send_emails(infra: Infrastructure, emails: list[OutgoingEmail]) -> None:
    """Best-effort delivery of already-queued notifications (never raises into the server)."""
    for message in emails:
        try:
            await infra.email.send(message)
        except Exception:
            log.warning("background_email_failed")
