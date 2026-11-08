"""Auth API contracts. Public schemas expose explicit fields only; ORM models never leak."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RegisterRequest(_Strict):
    email: str = Field(min_length=3, max_length=320)
    # The upper bound only protects the hasher from huge inputs; the real policy is in the service.
    password: str = Field(min_length=1, max_length=1024, repr=False)
    display_name: str | None = Field(default=None, max_length=100)


class LoginRequest(_Strict):
    email: str = Field(min_length=1, max_length=320)
    password: str = Field(min_length=1, max_length=1024, repr=False)


class RefreshRequest(_Strict):
    refresh_token: str = Field(min_length=20, max_length=512, repr=False)


class LogoutRequest(_Strict):
    refresh_token: str = Field(min_length=20, max_length=512, repr=False)


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"  # noqa: S105  (OAuth token type literal, not a secret)
    expires_in: int


class UserPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    display_name: str | None
    email_verified: bool
    created_at: datetime


class SessionPublic(BaseModel):
    id: uuid.UUID
    created_at: datetime
    last_used_at: datetime
    user_agent: str | None
    current: bool


class LoginResponse(BaseModel):
    """Either a token pair, or `mfa_required` with a short-lived `mfa_token` to finish login."""

    mfa_required: bool = False
    access_token: str | None = None
    refresh_token: str | None = None
    token_type: str = "bearer"  # noqa: S105  (OAuth token type literal, not a secret)
    expires_in: int | None = None
    mfa_token: str | None = None


class ChangePasswordRequest(_Strict):
    current_password: str = Field(min_length=1, max_length=1024, repr=False)
    new_password: str = Field(min_length=1, max_length=1024, repr=False)


class ForgotPasswordRequest(_Strict):
    email: str = Field(min_length=1, max_length=320)


class AcceptedResponse(BaseModel):
    message: str = "If the account exists, an email has been sent."


class ResetPasswordRequest(_Strict):
    token: str = Field(min_length=20, max_length=512, repr=False)
    new_password: str = Field(min_length=1, max_length=1024, repr=False)


class VerifyEmailRequest(_Strict):
    token: str = Field(min_length=20, max_length=512, repr=False)


class MfaSetupResponse(BaseModel):
    secret: str
    otpauth_uri: str


class MfaCodeRequest(_Strict):
    code: str = Field(min_length=6, max_length=32, repr=False)


class MfaRecoveryCodesResponse(BaseModel):
    recovery_codes: list[str]


class MfaVerifyRequest(_Strict):
    mfa_token: str = Field(min_length=20, max_length=2048, repr=False)
    code: str = Field(min_length=6, max_length=32, repr=False)


class MfaDisableRequest(_Strict):
    password: str = Field(min_length=1, max_length=1024, repr=False)
    code: str = Field(min_length=6, max_length=32, repr=False)


class MfaStatusResponse(BaseModel):
    enabled: bool
    recovery_codes_remaining: int


class DeleteAccountRequest(_Strict):
    password: str = Field(min_length=1, max_length=1024, repr=False)
    code: str | None = Field(default=None, max_length=32, repr=False)  # required when MFA is on


class DeletionAccepted(BaseModel):
    message: str = "Your account is locked and scheduled for permanent deletion."
