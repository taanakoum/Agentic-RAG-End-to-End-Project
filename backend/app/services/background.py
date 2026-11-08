"""Work that runs after the HTTP response (own DB session, never raises into the server)."""

from __future__ import annotations

import uuid

from app.core.logging import get_logger
from app.infra.container import Infrastructure
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
