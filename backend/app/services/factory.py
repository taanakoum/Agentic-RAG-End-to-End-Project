"""Builds services for a given DB session. Used by request dependencies and background tasks."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.infra.container import Infrastructure
from app.services.account_service import AccountService
from app.services.auth_service import AuthService
from app.services.mfa_service import MfaService


def build_auth_service(session: AsyncSession, infra: Infrastructure) -> AuthService:
    return AuthService(
        session=session,
        settings=infra.settings,
        passwords=infra.passwords,
        limiter=infra.rate_limiter,
    )


def build_account_service(session: AsyncSession, infra: Infrastructure) -> AccountService:
    return AccountService(
        session=session,
        settings=infra.settings,
        passwords=infra.passwords,
        limiter=infra.rate_limiter,
        email=infra.email,
    )


def build_mfa_service(session: AsyncSession, infra: Infrastructure) -> MfaService:
    return MfaService(
        session=session,
        settings=infra.settings,
        passwords=infra.passwords,
        limiter=infra.rate_limiter,
        auth=build_auth_service(session, infra),
    )
