"""Authentication business logic: registration, login, refresh rotation, logout, token auth."""

from __future__ import annotations

import hmac
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import (
    AuthenticationError,
    AuthorizationError,
    DuplicateResourceError,
    ErrorDetail,
    RequestValidationAppError,
)
from app.core.logging import get_logger
from app.core.ownership import require_owner
from app.core.security import (
    PASSWORD_MESSAGES,
    InvalidEmailError,
    PasswordService,
    TokenError,
    create_access_token,
    create_mfa_token,
    decode_access_token,
    generate_opaque_token,
    hash_opaque_token,
    normalize_email,
    password_policy_violations,
)
from app.infra.rate_limit import RateLimiter, subject_key
from app.models.user import User
from app.repositories.account import MfaRepository
from app.repositories.refresh_tokens import RefreshTokenRepository, SessionRow
from app.repositories.users import UserRepository

log = get_logger(__name__)

_EMAIL_UNIQUE_CONSTRAINT = "uq_users_email_normalized"


@dataclass(frozen=True, slots=True)
class IssuedTokens:
    access_token: str
    refresh_token: str
    expires_in: int


@dataclass(frozen=True, slots=True)
class MfaChallenge:
    """Password was correct but the account requires a second factor."""

    mfa_token: str
    expires_in: int


@dataclass(frozen=True, slots=True)
class AuthenticatedUser:
    """Identity derived from a validated token. The only source of user identity in requests."""

    id: uuid.UUID
    email: str
    session_id: uuid.UUID
    email_verified: bool = False


def _utc_now() -> datetime:
    return datetime.now(UTC)


class AuthService:
    def __init__(
        self,
        *,
        session: AsyncSession,
        settings: Settings,
        passwords: PasswordService,
        limiter: RateLimiter,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._session = session
        self._settings = settings
        self._passwords = passwords
        self._limiter = limiter
        self._clock = clock
        self._users = UserRepository(session)
        self._tokens = RefreshTokenRepository(session)

    # ------------------------------------------------------------ registration

    async def register(
        self,
        *,
        email: str,
        password: str,
        display_name: str | None,
        client_ip: str,
        invite_code: str | None = None,
    ) -> User:
        s = self._settings
        await self._limiter.hit(
            "register_ip",
            subject_key(client_ip),
            limit=s.rl_register_ip_limit,
            window_s=s.rl_register_ip_window_s,
        )
        required = s.registration_invite_code.get_secret_value()
        # Checked after the per-IP limit, so guessing the code is rate limited too.
        if required and not hmac.compare_digest((invite_code or "").encode(), required.encode()):
            raise AuthorizationError(
                "Sign-up on this deployment needs a valid invite code.", code="INVITE_REQUIRED"
            )
        try:
            normalized = normalize_email(email)
        except InvalidEmailError as exc:
            raise RequestValidationAppError(
                details=[ErrorDetail(field="email", code="invalid_email", message="Invalid email.")]
            ) from exc
        violations = password_policy_violations(password, email=normalized, settings=s)
        if violations:
            raise RequestValidationAppError(
                details=[
                    ErrorDetail(field="password", code=v, message=PASSWORD_MESSAGES[v])
                    for v in violations
                ]
            )
        name = display_name.strip() if display_name and display_name.strip() else None

        # Cheap pre-check to avoid hashing for known duplicates. The unique constraint below is
        # still the final authority (two concurrent requests can both pass this check).
        if await self._users.get_by_email_normalized(normalized) is not None:
            raise self._duplicate_email()

        password_hash = await self._passwords.hash(password)
        try:
            user = await self._users.add(
                email=email.strip(),
                email_normalized=normalized,
                password_hash=password_hash,
                display_name=name,
            )
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            if _EMAIL_UNIQUE_CONSTRAINT in str(exc.orig):
                raise self._duplicate_email() from exc
            raise
        log.info("user_registered", user_id=str(user.id))
        return user

    @staticmethod
    def _duplicate_email() -> DuplicateResourceError:
        return DuplicateResourceError(
            "An account with this email already exists.", code="EMAIL_ALREADY_REGISTERED"
        )

    # ------------------------------------------------------------ login

    async def login(
        self, *, email: str, password: str, client_ip: str, user_agent: str | None
    ) -> IssuedTokens | MfaChallenge:
        s = self._settings
        await self._limiter.hit(
            "login_ip",
            subject_key(client_ip),
            limit=s.rl_login_ip_limit,
            window_s=s.rl_login_ip_window_s,
        )
        try:
            normalized = normalize_email(email)
        except InvalidEmailError:
            normalized = email.strip().lower()[:320]
        # Keyed on the submitted identity whether or not the account exists (no enumeration).
        account_subject = subject_key(normalized)
        await self._limiter.check("login_account", account_subject, limit=s.rl_login_account_limit)

        user = await self._users.get_by_email_normalized(normalized)
        if user is None or not user.is_active:
            await self._passwords.verify_dummy(password)
            await self._record_login_failure(account_subject)
            raise self._invalid_credentials()
        if not await self._passwords.verify(user.password_hash, password):
            await self._record_login_failure(account_subject)
            raise self._invalid_credentials()

        await self._limiter.reset("login_account", account_subject)
        if self._passwords.needs_rehash(user.password_hash):
            user.password_hash = await self._passwords.hash(password)
        mfa = await MfaRepository(self._session).get(user.id)
        if mfa is not None and mfa.confirmed_at is not None:
            await self._session.commit()  # persists a possible rehash
            log.info("login_mfa_required", user_id=str(user.id))
            return MfaChallenge(
                mfa_token=create_mfa_token(s, user_id=user.id, now=self._clock()),
                expires_in=s.mfa_token_ttl_s,
            )
        tokens = await self.issue_session(user.id, user_agent=user_agent)
        await self._session.commit()
        log.info("login_succeeded", user_id=str(user.id))
        return tokens

    async def _record_login_failure(self, account_subject: str) -> None:
        await self._limiter.fail(
            "login_account", account_subject, window_s=self._settings.rl_login_account_window_s
        )

    @staticmethod
    def _invalid_credentials() -> AuthenticationError:
        return AuthenticationError("Invalid email or password.", code="INVALID_CREDENTIALS")

    async def issue_session(
        self, user_id: uuid.UUID, *, user_agent: str | None, family_id: uuid.UUID | None = None
    ) -> IssuedTokens:
        """Create a (new or continued) session. The caller owns the commit."""
        return await self._issue(
            user_id, family_id=family_id or uuid.uuid4(), user_agent=user_agent
        )

    async def _issue(
        self, user_id: uuid.UUID, *, family_id: uuid.UUID, user_agent: str | None
    ) -> IssuedTokens:
        now = self._clock()
        refresh = generate_opaque_token()
        await self._tokens.add(
            user_id=user_id,
            family_id=family_id,
            token_hash=hash_opaque_token(refresh),
            expires_at=now + timedelta(seconds=self._settings.refresh_token_ttl_s),
            user_agent=user_agent,
        )
        access = create_access_token(self._settings, user_id=user_id, session_id=family_id, now=now)
        return IssuedTokens(
            access_token=access,
            refresh_token=refresh,
            expires_in=self._settings.access_token_ttl_s,
        )

    # ------------------------------------------------------------ refresh / logout

    async def refresh(
        self, *, refresh_token: str, client_ip: str, user_agent: str | None
    ) -> IssuedTokens:
        s = self._settings
        await self._limiter.hit(
            "refresh_ip",
            subject_key(client_ip),
            limit=s.rl_refresh_ip_limit,
            window_s=s.rl_refresh_ip_window_s,
        )
        now = self._clock()
        # FOR UPDATE: two simultaneous refreshes of the same token serialize; the loser sees a
        # revoked token and is treated as reuse (family revoked). Strict by design.
        token = await self._tokens.get_by_hash_for_update(hash_opaque_token(refresh_token))
        if token is None:
            await self._session.rollback()
            raise self._invalid_refresh()
        if token.revoked_at is not None:
            await self._tokens.revoke_family(token.family_id, reason="reuse_detected", now=now)
            await self._session.commit()
            log.warning(
                "refresh_token_reuse_detected",
                user_id=str(token.user_id),
                family_id=str(token.family_id),
            )
            raise self._invalid_refresh()
        if token.expires_at <= now:
            await self._tokens.revoke(token, reason="expired", now=now)
            await self._session.commit()
            raise self._invalid_refresh()
        user = await self._users.get_by_id(token.user_id)
        if user is None or not user.is_active:
            await self._tokens.revoke_family(token.family_id, reason="user_inactive", now=now)
            await self._session.commit()
            raise self._invalid_refresh()

        await self._tokens.revoke(token, reason="rotated", now=now)
        tokens = await self._issue(user.id, family_id=token.family_id, user_agent=user_agent)
        await self._session.commit()
        return tokens

    @staticmethod
    def _invalid_refresh() -> AuthenticationError:
        return AuthenticationError(
            "Invalid or expired refresh token.", code="INVALID_REFRESH_TOKEN"
        )

    async def logout(self, *, refresh_token: str) -> None:
        """Revoke the whole session family. Idempotent: unknown tokens succeed silently."""
        token = await self._tokens.get_by_hash_for_update(hash_opaque_token(refresh_token))
        if token is not None:
            await self._tokens.revoke_family(token.family_id, reason="logout", now=self._clock())
            await self._session.commit()
            log.info("logout", user_id=str(token.user_id))
        else:
            await self._session.rollback()

    async def list_sessions(self, user: AuthenticatedUser) -> list[SessionRow]:
        return await self._tokens.list_active_sessions(user.id, now=self._clock())

    async def revoke_session(self, user: AuthenticatedUser, session_id: uuid.UUID) -> None:
        """Revoke one of the caller's own sessions. Anyone else's session is a 404."""
        require_owner(await self._tokens.family_owner(session_id), user.id)
        revoked = await self._tokens.revoke_family(
            session_id, reason="user_revoked", now=self._clock()
        )
        await self._session.commit()
        if revoked == 0:  # owned but already ended: indistinguishable from not found
            require_owner(None, user.id)

    async def get_profile(self, user_id: uuid.UUID) -> User:
        user = await self._users.get_by_id(user_id)
        if user is None:
            raise AuthenticationError("Invalid or expired access token.", code="INVALID_TOKEN")
        return user

    # ------------------------------------------------------------ access token authentication

    async def authenticate(self, access_token: str) -> AuthenticatedUser:
        try:
            claims = decode_access_token(self._settings, access_token)
        except TokenError as exc:
            raise AuthenticationError(
                "Invalid or expired access token.", code="INVALID_TOKEN"
            ) from exc
        row = await self._tokens.auth_row(claims.user_id, claims.session_id, now=self._clock())
        if row is None or not row.is_active:
            raise AuthenticationError("Invalid or expired access token.", code="INVALID_TOKEN")
        if not row.session_live:
            raise AuthenticationError("Session has ended.", code="SESSION_REVOKED")
        return AuthenticatedUser(
            id=row.user_id,
            email=row.email,
            session_id=claims.session_id,
            email_verified=row.email_verified,
        )
