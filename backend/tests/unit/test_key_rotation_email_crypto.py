from __future__ import annotations

import socket
import uuid
from datetime import UTC, datetime, timedelta
from email import message_from_bytes
from pathlib import Path
from typing import Any

import pytest
from aiosmtpd.controller import Controller
from aiosmtpd.handlers import Message
from cryptography.fernet import Fernet

from app.core.config import Settings
from app.core.crypto import SecretBox
from app.core.exceptions import ExternalServiceUnavailableError
from app.core.security import (
    TokenError,
    create_access_token,
    create_mfa_token,
    decode_access_token,
    decode_mfa_token,
)
from app.infra.email import FileEmailSender, OutgoingEmail, SmtpEmailSender
from tests.helpers import forge_jwt, unverified_header

KEY = "dGVzdC1vbmx5LWZlcm5ldC1rZXktMzItYnl0ZXMhISE="


def settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "app_env": "dev",
        "jwt_secret": "a" * 40,
        "mfa_encryption_key": KEY,
    }
    return Settings.build(**{**base, **overrides})


# ---------------------------------------------------------------- JWT key rotation


def test_new_tokens_carry_the_active_key_id() -> None:
    s = settings(jwt_key_id="k7")
    token = create_access_token(s, user_id=uuid.uuid4(), session_id=uuid.uuid4())
    assert unverified_header(token)["kid"] == "k7"
    assert unverified_header(token)["alg"] == "HS256"


def test_rotation_old_tokens_verify_until_the_retired_key_is_removed() -> None:
    uid, sid = uuid.uuid4(), uuid.uuid4()
    before = settings(jwt_key_id="k1", jwt_secret="a" * 40)
    old_token = create_access_token(before, user_id=uid, session_id=sid)

    # Operator rotates: new active key, old one kept for verification only.
    during = settings(jwt_key_id="k2", jwt_secret="b" * 40, jwt_retired_keys={"k1": "a" * 40})
    assert decode_access_token(during, old_token).user_id == uid
    new_token = create_access_token(during, user_id=uid, session_id=sid)
    assert unverified_header(new_token)["kid"] == "k2"
    assert decode_access_token(during, new_token).user_id == uid

    # After the access-token lifetime the retired key is dropped: old tokens stop working.
    after = settings(jwt_key_id="k2", jwt_secret="b" * 40)
    with pytest.raises(TokenError):
        decode_access_token(after, old_token)
    assert decode_access_token(after, new_token).user_id == uid


def test_unknown_key_id_rejected() -> None:
    s = settings()
    other = settings(jwt_key_id="zz", jwt_secret="c" * 40)
    token = create_access_token(other, user_id=uuid.uuid4(), session_id=uuid.uuid4())
    with pytest.raises(TokenError):
        decode_access_token(s, token)


def test_kid_cannot_be_used_to_pick_a_key_the_signature_does_not_match() -> None:
    """Header says kid=k1 (a valid retired key) but the signature was made with another secret."""
    s = settings(jwt_key_id="k2", jwt_secret="b" * 40, jwt_retired_keys={"k1": "a" * 40})
    forged = create_access_token(
        settings(jwt_key_id="k1", jwt_secret="x" * 40),
        user_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
    )
    assert unverified_header(forged)["kid"] == "k1"
    with pytest.raises(TokenError):
        decode_access_token(s, forged)


def test_legacy_token_without_kid_verifies_with_active_key_only() -> None:
    s = settings(jwt_key_id="k2", jwt_secret="b" * 40, jwt_retired_keys={"k1": "a" * 40})
    now = int(datetime.now(UTC).timestamp())
    payload = {
        "iss": s.jwt_issuer,
        "aud": s.jwt_audience,
        "sub": str(uuid.uuid4()),
        "sid": str(uuid.uuid4()),
        "type": "access",
        "jti": "x",
        "iat": now,
        "exp": now + 600,
    }
    assert decode_access_token(s, forge_jwt(payload, "b" * 40, "HS256")) is not None
    with pytest.raises(TokenError):  # signed with the retired key but without a kid: not accepted
        decode_access_token(s, forge_jwt(payload, "a" * 40, "HS256"))


def test_mfa_and_access_tokens_are_not_interchangeable() -> None:
    s = settings()
    uid = uuid.uuid4()
    mfa = create_mfa_token(s, user_id=uid)
    access = create_access_token(s, user_id=uid, session_id=uuid.uuid4())
    assert decode_mfa_token(s, mfa) == uid
    with pytest.raises(TokenError):
        decode_access_token(s, mfa)  # no sid claim and wrong type
    with pytest.raises(TokenError):
        decode_mfa_token(s, access)


def test_mfa_token_expires() -> None:
    s = settings()
    expired = create_mfa_token(s, user_id=uuid.uuid4(), now=datetime.now(UTC) - timedelta(hours=1))
    with pytest.raises(TokenError):
        decode_mfa_token(s, expired)


# ---------------------------------------------------------------- secret box


def test_secret_box_roundtrip_and_wrong_key() -> None:
    box = SecretBox(settings())
    token = box.encrypt("JBSWY3DPEHPK3PXP")
    assert "JBSWY3DPEHPK3PXP" not in token
    assert box.decrypt(token) == "JBSWY3DPEHPK3PXP"
    other = SecretBox(settings(mfa_encryption_key=Fernet.generate_key().decode()))
    assert other.decrypt(token) is None
    assert box.decrypt("garbage") is None


# ---------------------------------------------------------------- email senders


async def test_file_email_sender_writes_parseable_eml(tmp_path: Path) -> None:
    s = settings(email_file_dir=tmp_path / "out", email_from="noreply@example.org")
    await FileEmailSender(s).send(OutgoingEmail(to="a@b.com", subject="Hi", body="Token: abc"))
    (path,) = list((tmp_path / "out").glob("*.eml"))
    msg = message_from_bytes(path.read_bytes())
    assert (msg["To"], msg["From"], msg["Subject"]) == ("a@b.com", "noreply@example.org", "Hi")
    assert "Token: abc" in msg.get_payload()


class _Collector(Message):
    def __init__(self) -> None:
        super().__init__()
        self.received: list[Any] = []

    def handle_message(self, message: Any) -> None:
        self.received.append(message)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def test_smtp_sender_delivers_to_a_real_smtp_server() -> None:
    handler = _Collector()
    port = _free_port()
    controller = Controller(handler, hostname="127.0.0.1", port=port)
    controller.start()
    try:
        s = settings(
            email_backend="smtp",
            smtp_host="127.0.0.1",
            smtp_port=port,
            smtp_starttls=False,
            email_from="noreply@example.org",
        )
        await SmtpEmailSender(s).send(
            OutgoingEmail(to="user@example.com", subject="Real SMTP", body="Hello over SMTP")
        )
    finally:
        controller.stop()
    assert len(handler.received) == 1
    msg = handler.received[0]
    assert msg["To"] == "user@example.com"
    assert msg["Subject"] == "Real SMTP"
    assert "Hello over SMTP" in msg.get_payload()


async def test_smtp_sender_failure_is_a_clean_domain_error() -> None:
    s = settings(
        email_backend="smtp", smtp_host="127.0.0.1", smtp_port=_free_port(), smtp_starttls=False
    )
    with pytest.raises(ExternalServiceUnavailableError):
        await SmtpEmailSender(s).send(OutgoingEmail(to="a@b.com", subject="x", body="secret-token"))
