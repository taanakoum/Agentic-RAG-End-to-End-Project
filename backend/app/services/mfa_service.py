"""TOTP multi-factor authentication (RFC 6238) with single-use recovery codes."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import pyotp
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.crypto import SecretBox
from app.core.exceptions import (
    AuthenticationError,
    DuplicateResourceError,
    ErrorDetail,
    MalformedRequestError,
    RequestValidationAppError,
)
from app.core.logging import get_logger
from app.core.security import PasswordService, TokenError, decode_mfa_token
from app.infra.rate_limit import RateLimiter, subject_key
from app.repositories.account import MfaRepository
from app.repositories.users import UserRepository
from app.services.auth_service import AuthenticatedUser, AuthService, IssuedTokens

log = get_logger(__name__)

_STEP_SECONDS = 30
_TOTP_RE = re.compile(r"^\d{6}$")
_RECOVERY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I


@dataclass(frozen=True, slots=True)
class MfaSetup:
    secret: str
    otpauth_uri: str


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _normalize_recovery(code: str) -> str:
    return re.sub(r"[\s-]", "", code).upper()


def _hash_recovery(code: str) -> str:
    return hashlib.sha256(_normalize_recovery(code).encode()).hexdigest()


def _new_recovery_code() -> str:
    raw = "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(10))
    return f"{raw[:5]}-{raw[5:]}"


class MfaService:
    def __init__(
        self,
        *,
        session: AsyncSession,
        settings: Settings,
        passwords: PasswordService,
        limiter: RateLimiter,
        auth: AuthService,
        clock: Callable[[], datetime] = _utc_now,
        time_source: Callable[[], float] = time.time,
    ) -> None:
        self._session = session
        self._settings = settings
        self._passwords = passwords
        self._limiter = limiter
        self._auth = auth
        self._clock = clock
        self._time = time_source
        self._mfa = MfaRepository(session)
        self._users = UserRepository(session)
        self._box = SecretBox(settings)

    # ------------------------------------------------------------ enrollment

    async def status(self, user: AuthenticatedUser) -> tuple[bool, int]:
        row = await self._mfa.get(user.id)
        if row is None or row.confirmed_at is None:
            return False, 0
        return True, await self._mfa.remaining_recovery_codes(user.id)

    async def setup(self, user: AuthenticatedUser) -> MfaSetup:
        existing = await self._mfa.get(user.id)
        if existing is not None and existing.confirmed_at is not None:
            raise DuplicateResourceError(
                "Two-factor authentication is already enabled.", code="MFA_ALREADY_ENABLED"
            )
        secret = pyotp.random_base32()
        await self._mfa.upsert_unconfirmed(user.id, self._box.encrypt(secret))
        await self._session.commit()
        # pyotp's provisioning_uri is only partially annotated.
        uri: str = pyotp.TOTP(secret).provisioning_uri(  # pyright: ignore[reportUnknownMemberType]
            name=user.email, issuer_name=self._settings.mfa_issuer
        )
        return MfaSetup(secret=secret, otpauth_uri=uri)

    async def confirm(self, user: AuthenticatedUser, *, code: str) -> list[str]:
        """Activate MFA after the first valid code. Returns the recovery codes (shown once)."""
        s = self._settings
        subject = subject_key("mfa", str(user.id))
        await self._limiter.check("mfa", subject, limit=s.rl_mfa_limit)
        row = await self._mfa.get(user.id)
        if row is None or row.confirmed_at is not None:
            raise MalformedRequestError(
                "No pending two-factor setup. Start setup first.", code="MFA_NOT_PENDING"
            )
        step = self._match_totp(row.secret_encrypted, code)
        if step is None or not await self._mfa.advance_step(user.id, step):
            await self._limiter.fail("mfa", subject, window_s=s.rl_mfa_window_s)
            raise self._invalid_code()
        await self._mfa.confirm(user.id, now=self._clock())
        codes = [_new_recovery_code() for _ in range(s.mfa_recovery_code_count)]
        await self._mfa.replace_recovery_codes(user.id, [_hash_recovery(c) for c in codes])
        await self._session.commit()
        await self._limiter.reset("mfa", subject)
        log.info("mfa_enabled", user_id=str(user.id))
        return codes

    async def disable(self, user: AuthenticatedUser, *, password: str, code: str) -> None:
        s = self._settings
        subject = subject_key("mfa", str(user.id))
        await self._limiter.check("mfa", subject, limit=s.rl_mfa_limit)
        row = await self._users.get_by_id(user.id)
        if row is None or not await self._passwords.verify(row.password_hash, password):
            await self._limiter.fail("mfa", subject, window_s=s.rl_mfa_window_s)
            raise RequestValidationAppError(
                details=[
                    ErrorDetail(
                        field="password", code="incorrect_password", message="Incorrect password."
                    )
                ]
            )
        if not await self._check_second_factor(user.id, code):
            await self._limiter.fail("mfa", subject, window_s=s.rl_mfa_window_s)
            raise self._invalid_code()
        await self._mfa.delete_all(user.id)
        await self._session.commit()
        await self._limiter.reset("mfa", subject)
        log.info("mfa_disabled", user_id=str(user.id))

    # ------------------------------------------------------------ login completion

    async def complete_login(
        self, *, mfa_token: str, code: str, user_agent: str | None
    ) -> IssuedTokens:
        s = self._settings
        try:
            user_id = decode_mfa_token(s, mfa_token)
        except TokenError as exc:
            raise AuthenticationError(
                "The two-factor challenge is invalid or has expired.", code="INVALID_MFA_TOKEN"
            ) from exc
        subject = subject_key("mfa", str(user_id))
        await self._limiter.check("mfa", subject, limit=s.rl_mfa_limit)
        user = await self._users.get_by_id(user_id)
        if user is None or not user.is_active:
            raise AuthenticationError(
                "The two-factor challenge is invalid or has expired.", code="INVALID_MFA_TOKEN"
            )
        if not await self._check_second_factor(user_id, code):
            await self._limiter.fail("mfa", subject, window_s=s.rl_mfa_window_s)
            await self._session.commit()  # keep nothing half-done
            raise self._invalid_code()
        await self._limiter.reset("mfa", subject)
        tokens = await self._auth.issue_session(user_id, user_agent=user_agent)
        await self._session.commit()
        log.info("login_succeeded_mfa", user_id=str(user_id))
        return tokens

    # ------------------------------------------------------------ verification primitives

    def _match_totp(self, secret_encrypted: str, code: str) -> int | None:
        """The TOTP step the code belongs to (current +/- 1), or None."""
        candidate = code.strip().replace(" ", "")
        secret = self._box.decrypt(secret_encrypted)
        if secret is None or not _TOTP_RE.fullmatch(candidate):
            return None
        totp = pyotp.TOTP(secret, interval=_STEP_SECONDS)
        current = int(self._time() // _STEP_SECONDS)
        matched: int | None = None
        for step in (current - 1, current, current + 1):  # constant work, no early exit
            if hmac.compare_digest(totp.generate_otp(step), candidate):
                matched = step
        return matched

    async def _check_second_factor(self, user_id: uuid.UUID, code: str) -> bool:
        """Accept a TOTP code (once per time step) or an unused recovery code."""
        row = await self._mfa.get(user_id)
        if row is None or row.confirmed_at is None:
            return False
        step = self._match_totp(row.secret_encrypted, code)
        if step is not None:
            return await self._mfa.advance_step(user_id, step)
        if _TOTP_RE.fullmatch(code.strip().replace(" ", "")):
            return False  # looked like a TOTP code and was wrong; do not burn recovery lookups
        return await self._mfa.consume_recovery_code(
            user_id, _hash_recovery(code), now=self._clock()
        )

    @staticmethod
    def _invalid_code() -> AuthenticationError:
        return AuthenticationError("The verification code is invalid.", code="INVALID_MFA_CODE")
