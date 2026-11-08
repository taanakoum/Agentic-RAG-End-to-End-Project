"""Symmetric encryption of secrets at rest (TOTP seeds) with Fernet (AES-128-CBC + HMAC)."""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import Settings


class SecretBox:
    def __init__(self, settings: Settings) -> None:
        self._fernet = Fernet(settings.mfa_encryption_key.get_secret_value().encode())

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, token: str) -> str | None:
        """Plaintext, or None if the token was not produced with this key (e.g. key changed)."""
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except InvalidToken:
            return None
