"""Auth endpoints. HTTP concerns only; logic lives in AuthService."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Header, Response, status

from app.core.exceptions import error_responses
from app.dependencies.auth import AuthServiceDep, ClientIpDep, CurrentUser, InfraDep
from app.schemas.auth import (
    LoginRequest,
    LoginResponse,
    LogoutRequest,
    RefreshRequest,
    RegisterRequest,
    SessionPublic,
    TokenResponse,
    UserPublic,
)
from app.services.auth_service import MfaChallenge
from app.services.background import send_verification_email

router = APIRouter(prefix="/auth", tags=["auth"])

UserAgent = Annotated[str | None, Header(alias="User-Agent")]


@router.post(
    "/register",
    response_model=UserPublic,
    status_code=status.HTTP_201_CREATED,
    responses=error_responses(409, 422, 429, 503),
)
async def register(
    body: RegisterRequest,
    service: AuthServiceDep,
    ip: ClientIpDep,
    infra: InfraDep,
    background: BackgroundTasks,
) -> UserPublic:
    user = await service.register(
        email=body.email, password=body.password, display_name=body.display_name, client_ip=ip
    )
    background.add_task(send_verification_email, infra, user.id)
    return UserPublic.model_validate(user)


@router.post("/login", response_model=LoginResponse, responses=error_responses(401, 422, 429, 503))
async def login(
    body: LoginRequest, service: AuthServiceDep, ip: ClientIpDep, user_agent: UserAgent = None
) -> LoginResponse:
    tokens = await service.login(
        email=body.email, password=body.password, client_ip=ip, user_agent=user_agent
    )
    if isinstance(tokens, MfaChallenge):
        return LoginResponse(
            mfa_required=True, mfa_token=tokens.mfa_token, expires_in=tokens.expires_in
        )
    return LoginResponse(
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
        expires_in=tokens.expires_in,
    )


@router.post(
    "/refresh", response_model=TokenResponse, responses=error_responses(401, 422, 429, 503)
)
async def refresh(
    body: RefreshRequest, service: AuthServiceDep, ip: ClientIpDep, user_agent: UserAgent = None
) -> TokenResponse:
    tokens = await service.refresh(
        refresh_token=body.refresh_token, client_ip=ip, user_agent=user_agent
    )
    return TokenResponse(
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
        expires_in=tokens.expires_in,
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT, responses=error_responses(422, 503))
async def logout(body: LogoutRequest, service: AuthServiceDep) -> Response:
    await service.logout(refresh_token=body.refresh_token)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/me", response_model=UserPublic, responses=error_responses(401))
async def me(current: CurrentUser, service: AuthServiceDep) -> UserPublic:
    return UserPublic.model_validate(await service.get_profile(current.id))


@router.get("/sessions", response_model=list[SessionPublic], responses=error_responses(401))
async def list_sessions(current: CurrentUser, service: AuthServiceDep) -> list[SessionPublic]:
    rows = await service.list_sessions(current)
    return [
        SessionPublic(
            id=r.family_id,
            created_at=r.created_at,
            last_used_at=r.last_used_at,
            user_agent=r.user_agent,
            current=r.family_id == current.session_id,
        )
        for r in rows
    ]


@router.delete(
    "/sessions/{session_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=error_responses(401, 404, 422),
)
async def revoke_session(
    session_id: uuid.UUID, current: CurrentUser, service: AuthServiceDep
) -> Response:
    await service.revoke_session(current, session_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
