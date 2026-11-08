"""Outgoing email. `file` backend writes .eml files (dev/test); `smtp` sends for real."""

from __future__ import annotations

import asyncio
import smtplib
import ssl
import uuid
from dataclasses import dataclass
from email.message import EmailMessage as MimeMessage
from pathlib import Path
from typing import Protocol

from app.core.config import Settings
from app.core.exceptions import ExternalServiceUnavailableError
from app.core.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class OutgoingEmail:
    to: str
    subject: str
    body: str


class EmailSender(Protocol):
    async def send(self, message: OutgoingEmail) -> None: ...


def _mime(settings: Settings, message: OutgoingEmail) -> MimeMessage:
    mime = MimeMessage()
    mime["From"] = settings.email_from
    mime["To"] = message.to
    mime["Subject"] = message.subject
    mime.set_content(message.body)
    return mime


class FileEmailSender:
    """Writes each message to `<dir>/<uuid>.eml`. Dev/test only (prod config forbids it)."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._dir: Path = settings.email_file_dir
        self._dir.mkdir(parents=True, exist_ok=True)

    async def send(self, message: OutgoingEmail) -> None:
        data = _mime(self._settings, message).as_bytes()
        path = self._dir / f"{uuid.uuid4().hex}.eml"
        await asyncio.to_thread(path.write_bytes, data)


class SmtpEmailSender:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def _send_sync(self, mime: MimeMessage) -> None:
        s = self._settings
        if s.smtp_use_ssl:
            client: smtplib.SMTP = smtplib.SMTP_SSL(
                s.smtp_host,
                s.smtp_port,
                timeout=s.smtp_timeout_s,
                context=ssl.create_default_context(),
            )
        else:
            client = smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=s.smtp_timeout_s)
        with client:
            if s.smtp_starttls and not s.smtp_use_ssl:
                client.starttls(context=ssl.create_default_context())
            if s.smtp_username:
                client.login(s.smtp_username, s.smtp_password.get_secret_value())
            client.send_message(mime)

    async def send(self, message: OutgoingEmail) -> None:
        mime = _mime(self._settings, message)
        try:
            await asyncio.to_thread(self._send_sync, mime)
        except (smtplib.SMTPException, OSError) as exc:
            # Never log the message (it contains single-use tokens).
            log.error("email_send_failed", error=type(exc).__name__)
            raise ExternalServiceUnavailableError("Email delivery failed.") from exc


def create_email_sender(settings: Settings) -> EmailSender:
    if settings.email_backend == "smtp":
        return SmtpEmailSender(settings)
    return FileEmailSender(settings)
