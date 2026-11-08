"""Account security flows: password change/reset and email verification."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import ErrorDetail, MalformedRequestError, RequestValidationAppError
from app.core.logging import get_logger
from app.core.security import (
    InvalidEmailError,
    PasswordService,
    generate_opaque_token,
    hash_opaque_token,
    normalize_email,
    password_policy_violations,
)
from app.infra.email import EmailSender, OutgoingEmail
from app.infra.rate_limit import RateLimiter, subject_key
from app.models.one_time_token import PURPOSE_EMAIL_VERIFY, PURPOSE_PASSWORD_RESET
from app.repositories.account import OneTimeTokenRepository
from app.repositories.refresh_tokens import RefreshTokenRepository
from app.repositories.users import UserRepository
from app.services.auth_service import AuthenticatedUser

log = get_logger(__name__)

_POLICY_MESSAGES = {
    "password_too_short": "Password is too short.",
    "password_too_long": "Password is too long.",
    "password_blank": "Password must not be blank.",
    "password_too_repetitive": "Password is too repetitive.",
    "password_too_guessable": "Password is too easy to guess.",
}


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _policy_error(violations: list[str], field: str) -> RequestValidationAppError:
    return RequestValidationAppError(
        details=[
            ErrorDetail(field=field, code=v, message=_POLICY_MESSAGES.get(v, "Invalid password."))
            for v in violations
        ]
    )


class AccountService:
    def __init__(
        self,
        *,
        session: AsyncSession,
        settings: Settings,
        passwords: PasswordService,
        limiter: RateLimiter,
        email: EmailSender,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._session = session
        self._settings = settings
        self._passwords = passwords
        self._limiter = limiter
        self._email = email
        self._clock = clock
        self._users = UserRepository(session)
        self._sessions = RefreshTokenRepository(session)
        self._tokens = OneTimeTokenRepository(session)

    # ------------------------------------------------------------ change password

    async def change_password(
        self, user: AuthenticatedUser, *, current_password: str, new_password: str
    ) -> None:
        s = self._settings
        subject = subject_key(str(user.id))
        await self._limiter.check("pwchange", subject, limit=s.rl_pwchange_limit)
        row = await self._users.get_by_id(user.id)
        if row is None:
            raise MalformedRequestError()
        if not await self._passwords.verify(row.password_hash, current_password):
            await self._limiter.fail("pwchange", subject, window_s=s.rl_pwchange_window_s)
            raise RequestValidationAppError(
                details=[
                    ErrorDetail(
                        field="current_password",
                        code="incorrect_password",
                        message="Current password is incorrect.",
                    )
                ]
            )
        violations = password_policy_violations(
            new_password, email=row.email_normalized, settings=s
        )
        if new_password == current_password:
            violations.append("password_too_guessable")
        if violations:
            raise _policy_error(violations, "new_password")
        new_hash = await self._passwords.hash(new_password)
        now = self._clock()
        await self._users.set_password_hash(user.id, new_hash)
        # Every other device is signed out; the session that made the change stays.
        await self._sessions.revoke_all_for_user(
            user.id, reason="password_changed", now=now, keep_family=user.session_id
        )
        await self._session.commit()
        await self._limiter.reset("pwchange", subject)
        log.info("password_changed", user_id=str(user.id))
        await self._notify(
            row.email,
            "Your password was changed",
            (
                "The password for your account was just changed and your other devices were signed "
                "out. If this was not you, reset your password immediately."
            ),
        )

    # ------------------------------------------------------------ forgot / reset

    async def throttle_forgot(self, *, email: str, client_ip: str) -> None:
        """Synchronous (in-request) rate limits; the response never depends on the account."""
        s = self._settings
        await self._limiter.hit(
            "forgot_ip",
            subject_key(client_ip),
            limit=s.rl_forgot_ip_limit,
            window_s=s.rl_forgot_ip_window_s,
        )
        try:
            identity = normalize_email(email)
        except InvalidEmailError:
            identity = email.strip().lower()[:320]
        await self._limiter.hit(
            "forgot_account",
            subject_key(identity),
            limit=s.rl_forgot_account_limit,
            window_s=s.rl_forgot_account_window_s,
        )

    async def send_password_reset(self, email: str) -> None:
        """Runs after the HTTP response (background). Silent for unknown/inactive accounts."""
        try:
            normalized = normalize_email(email)
        except InvalidEmailError:
            return
        user = await self._users.get_by_email_normalized(normalized)
        if user is None or not user.is_active:
            return
        token = await self._new_token(
            user.id, PURPOSE_PASSWORD_RESET, self._settings.reset_token_ttl_s
        )
        await self._session.commit()
        minutes = self._settings.reset_token_ttl_s // 60
        link = f"{self._settings.app_base_url}/?reset_token={token}"
        await self._email.send(
            OutgoingEmail(
                to=user.email,
                subject="Reset your password",
                body=(
                    "Someone asked to reset the password for this account.\n\n"
                    f"Reset token: {token}\nLink: {link}\n\n"
                    f"It works once and expires in {minutes} minutes. "
                    "If this was not you, ignore this email."
                ),
            )
        )
        log.info("password_reset_sent", user_id=str(user.id))

    async def reset_password(self, *, token: str, new_password: str, client_ip: str) -> None:
        s = self._settings
        await self._limiter.hit(
            "reset_ip",
            subject_key(client_ip),
            limit=s.rl_reset_ip_limit,
            window_s=s.rl_reset_ip_window_s,
        )
        now = self._clock()
        # Consuming locks the token row until commit, so concurrent use of one token is serialized.
        user_id = await self._tokens.consume(
            hash_opaque_token(token), PURPOSE_PASSWORD_RESET, now=now
        )
        if user_id is None:
            await self._session.rollback()
            raise MalformedRequestError(
                "The reset token is invalid or has expired.", code="INVALID_RESET_TOKEN"
            )
        user = await self._users.get_by_id(user_id)
        if user is None or not user.is_active:
            await self._session.rollback()
            raise MalformedRequestError(
                "The reset token is invalid or has expired.", code="INVALID_RESET_TOKEN"
            )
        violations = password_policy_violations(
            new_password, email=user.email_normalized, settings=s
        )
        if violations:
            await self._session.rollback()  # the token stays usable for a corrected password
            raise _policy_error(violations, "new_password")
        await self._users.set_password_hash(user_id, await self._passwords.hash(new_password))
        await self._sessions.revoke_all_for_user(user_id, reason="password_reset", now=now)
        await self._session.commit()
        log.info("password_reset_completed", user_id=str(user_id))
        await self._notify(
            user.email,
            "Your password was reset",
            ("The password for your account was reset and all devices were signed out."),
        )

    # ------------------------------------------------------------ email verification

    async def send_verification(self, user_id: uuid.UUID) -> None:
        user = await self._users.get_by_id(user_id)
        if user is None or user.email_verified_at is not None:
            return
        token = await self._new_token(
            user.id, PURPOSE_EMAIL_VERIFY, self._settings.verify_token_ttl_s
        )
        await self._session.commit()
        link = f"{self._settings.app_base_url}/?verify_token={token}"
        await self._email.send(
            OutgoingEmail(
                to=user.email,
                subject="Verify your email address",
                body=(
                    f"Verification token: {token}\nLink: {link}\n\n"
                    "It works once and expires in 24 hours."
                ),
            )
        )
        log.info("verification_email_sent", user_id=str(user.id))

    async def throttle_verification_request(self, user: AuthenticatedUser) -> None:
        s = self._settings
        await self._limiter.hit(
            "verify_req",
            subject_key(str(user.id)),
            limit=s.rl_forgot_account_limit,
            window_s=s.rl_forgot_account_window_s,
        )

    async def verify_email(self, *, token: str, client_ip: str) -> None:
        s = self._settings
        await self._limiter.hit(
            "verify_ip",
            subject_key(client_ip),
            limit=s.rl_reset_ip_limit,
            window_s=s.rl_reset_ip_window_s,
        )
        now = self._clock()
        user_id = await self._tokens.consume(
            hash_opaque_token(token), PURPOSE_EMAIL_VERIFY, now=now
        )
        if user_id is None:
            await self._session.rollback()
            raise MalformedRequestError(
                "The verification token is invalid or has expired.",
                code="INVALID_VERIFICATION_TOKEN",
            )
        await self._users.mark_email_verified(user_id, now=now)
        await self._session.commit()
        log.info("email_verified", user_id=str(user_id))

    # ------------------------------------------------------------ helpers

    async def _new_token(self, user_id: uuid.UUID, purpose: str, ttl_s: int) -> str:
        now = self._clock()
        await self._tokens.invalidate_unused(user_id, purpose, now=now)  # only the newest works
        token = generate_opaque_token()
        await self._tokens.add(
            user_id=user_id,
            purpose=purpose,
            token_hash=hash_opaque_token(token),
            expires_at=now + timedelta(seconds=ttl_s),
        )
        return token

    async def _notify(self, to: str, subject: str, body: str) -> None:
        """Best-effort security notification; failures must not undo the completed change."""
        try:
            await self._email.send(OutgoingEmail(to=to, subject=subject, body=body))
        except Exception:
            log.warning("security_notification_failed")
