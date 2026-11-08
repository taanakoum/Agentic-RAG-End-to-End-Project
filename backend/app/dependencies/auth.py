"""Auth dependencies. User identity comes only from the validated bearer token."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AuthenticationError
from app.dependencies.infra import get_db_session, get_infra
from app.infra.container import Infrastructure
from app.services.auth_service import AuthenticatedUser, AuthService

# auto_error=False so that a missing header yields our standard 401 error body.
_bearer = HTTPBearer(auto_error=False)

SessionDep = Annotated[AsyncSession, Depends(get_db_session)]
InfraDep = Annotated[Infrastructure, Depends(get_infra)]


def get_auth_service(session: SessionDep, infra: InfraDep) -> AuthService:
    return AuthService(
        session=session,
        settings=infra.settings,
        passwords=infra.passwords,
        limiter=infra.rate_limiter,
    )


AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]


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
