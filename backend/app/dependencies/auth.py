"""Auth dependencies. User identity comes only from the validated bearer token."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AuthenticationError, AuthorizationError
from app.dependencies.infra import get_db_session, get_infra
from app.infra.container import Infrastructure
from app.services.account_service import AccountService
from app.services.auth_service import AuthenticatedUser, AuthService
from app.services.document_service import DocumentService
from app.services.factory import (
    build_account_service,
    build_auth_service,
    build_document_service,
    build_mfa_service,
    build_retrieval_service,
)
from app.services.mfa_service import MfaService
from app.services.retrieval_service import RetrievalService

# auto_error=False so that a missing header yields our standard 401 error body.
_bearer = HTTPBearer(auto_error=False)

SessionDep = Annotated[AsyncSession, Depends(get_db_session)]
InfraDep = Annotated[Infrastructure, Depends(get_infra)]


def get_auth_service(session: SessionDep, infra: InfraDep) -> AuthService:
    return build_auth_service(session, infra)


def get_account_service(session: SessionDep, infra: InfraDep) -> AccountService:
    return build_account_service(session, infra)


def get_mfa_service(session: SessionDep, infra: InfraDep) -> MfaService:
    return build_mfa_service(session, infra)


def get_document_service(session: SessionDep, infra: InfraDep) -> DocumentService:
    return build_document_service(session, infra)


def get_retrieval_service(session: SessionDep, infra: InfraDep) -> RetrievalService:
    return build_retrieval_service(session, infra)


AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]
DocumentServiceDep = Annotated[DocumentService, Depends(get_document_service)]
AccountServiceDep = Annotated[AccountService, Depends(get_account_service)]
MfaServiceDep = Annotated[MfaService, Depends(get_mfa_service)]
RetrievalServiceDep = Annotated[RetrievalService, Depends(get_retrieval_service)]


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


ClientIpDep = Annotated[str, Depends(client_ip)]


async def get_current_user(
    service: AuthServiceDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> AuthenticatedUser:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise AuthenticationError("Authentication required.", code="AUTHENTICATION_REQUIRED")
    return await service.authenticate(credentials.credentials)


CurrentUser = Annotated[AuthenticatedUser, Depends(get_current_user)]


async def get_verified_user(current: CurrentUser, infra: InfraDep) -> AuthenticatedUser:
    """Like CurrentUser, but when REQUIRE_VERIFIED_EMAIL is on the email must be verified."""
    if infra.settings.require_verified_email and not current.email_verified:
        raise AuthorizationError(
            "Verify your email address to use this feature.", code="EMAIL_NOT_VERIFIED"
        )
    return current


VerifiedUser = Annotated[AuthenticatedUser, Depends(get_verified_user)]
