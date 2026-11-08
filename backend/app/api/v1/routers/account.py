"""Password change/reset and email verification endpoints."""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Response, status

from app.core.exceptions import error_responses
from app.dependencies.auth import (
    AccountServiceDep,
    ClientIpDep,
    CurrentUser,
    InfraDep,
    MfaServiceDep,
)
from app.schemas.auth import (
    AcceptedResponse,
    ChangePasswordRequest,
    DeleteAccountRequest,
    DeletionAccepted,
    ForgotPasswordRequest,
    ResetPasswordRequest,
    VerifyEmailRequest,
)
from app.services.background import (
    kick_dispatcher,
    send_emails,
    send_password_reset_email,
    send_verification_email,
)

router = APIRouter(prefix="/auth", tags=["account"])


@router.post(
    "/password/change",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=error_responses(401, 422, 429, 503),
)
async def change_password(
    body: ChangePasswordRequest,
    current: CurrentUser,
    service: AccountServiceDep,
    infra: InfraDep,
    background: BackgroundTasks,
) -> Response:
    await service.change_password(
        current, current_password=body.current_password, new_password=body.new_password
    )
    background.add_task(send_emails, infra, service.take_notifications())
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/password/forgot",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=AcceptedResponse,
    responses=error_responses(422, 429, 503),
)
async def forgot_password(
    body: ForgotPasswordRequest,
    service: AccountServiceDep,
    ip: ClientIpDep,
    infra: InfraDep,
    background: BackgroundTasks,
) -> AcceptedResponse:
    """Always 202: the response never reveals whether the email has an account."""
    await service.throttle_forgot(email=body.email, client_ip=ip)
    background.add_task(send_password_reset_email, infra, body.email)
    return AcceptedResponse()


@router.post(
    "/password/reset",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=error_responses(400, 422, 429, 503),
)
async def reset_password(
    body: ResetPasswordRequest,
    service: AccountServiceDep,
    ip: ClientIpDep,
    infra: InfraDep,
    background: BackgroundTasks,
) -> Response:
    await service.reset_password(token=body.token, new_password=body.new_password, client_ip=ip)
    background.add_task(send_emails, infra, service.take_notifications())
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/email/verify/request",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=AcceptedResponse,
    responses=error_responses(401, 429, 503),
)
async def request_email_verification(
    current: CurrentUser,
    service: AccountServiceDep,
    infra: InfraDep,
    background: BackgroundTasks,
) -> AcceptedResponse:
    await service.throttle_verification_request(current)
    background.add_task(send_verification_email, infra, current.id)
    return AcceptedResponse(message="A verification email has been sent if it is still needed.")


@router.post(
    "/email/verify",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=error_responses(400, 422, 429, 503),
)
async def verify_email(
    body: VerifyEmailRequest, service: AccountServiceDep, ip: ClientIpDep
) -> Response:
    await service.verify_email(token=body.token, client_ip=ip)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete(
    "/account",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=DeletionAccepted,
    responses=error_responses(401, 422, 429, 503),
)
async def delete_account(
    body: DeleteAccountRequest,
    current: CurrentUser,
    service: AccountServiceDep,
    mfa: MfaServiceDep,
    infra: InfraDep,
    background: BackgroundTasks,
) -> DeletionAccepted:
    """Permanently delete the account and all its data. Needs the password (and an MFA code)."""
    await mfa.require_second_factor(current, body.code)
    await service.delete_account(current, password=body.password)
    background.add_task(send_emails, infra, service.take_notifications())
    background.add_task(kick_dispatcher, infra)
    return DeletionAccepted()
