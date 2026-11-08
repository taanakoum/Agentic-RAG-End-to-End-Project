"""TOTP multi-factor authentication endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, Response, status

from app.core.exceptions import error_responses
from app.dependencies.auth import CurrentUser, MfaServiceDep
from app.schemas.auth import (
    LoginResponse,
    MfaCodeRequest,
    MfaDisableRequest,
    MfaRecoveryCodesResponse,
    MfaSetupResponse,
    MfaStatusResponse,
    MfaVerifyRequest,
)

router = APIRouter(prefix="/auth/mfa", tags=["mfa"])

UserAgent = Annotated[str | None, Header(alias="User-Agent")]


@router.get("/status", response_model=MfaStatusResponse, responses=error_responses(401))
async def mfa_status(current: CurrentUser, service: MfaServiceDep) -> MfaStatusResponse:
    enabled, remaining = await service.status(current)
    return MfaStatusResponse(enabled=enabled, recovery_codes_remaining=remaining)


@router.post("/setup", response_model=MfaSetupResponse, responses=error_responses(401, 409))
async def mfa_setup(current: CurrentUser, service: MfaServiceDep) -> MfaSetupResponse:
    setup = await service.setup(current)
    return MfaSetupResponse(secret=setup.secret, otpauth_uri=setup.otpauth_uri)


@router.post(
    "/confirm",
    response_model=MfaRecoveryCodesResponse,
    responses=error_responses(400, 401, 422, 429),
)
async def mfa_confirm(
    body: MfaCodeRequest, current: CurrentUser, service: MfaServiceDep
) -> MfaRecoveryCodesResponse:
    return MfaRecoveryCodesResponse(recovery_codes=await service.confirm(current, code=body.code))


@router.post("/verify", response_model=LoginResponse, responses=error_responses(401, 422, 429))
async def mfa_verify(
    body: MfaVerifyRequest, service: MfaServiceDep, user_agent: UserAgent = None
) -> LoginResponse:
    tokens = await service.complete_login(
        mfa_token=body.mfa_token, code=body.code, user_agent=user_agent
    )
    return LoginResponse(
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
        expires_in=tokens.expires_in,
    )


@router.post(
    "/disable",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=error_responses(401, 422, 429),
)
async def mfa_disable(
    body: MfaDisableRequest, current: CurrentUser, service: MfaServiceDep
) -> Response:
    await service.disable(current, password=body.password, code=body.code)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
