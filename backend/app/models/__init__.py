"""Import all model modules so Alembic autogenerate and metadata see them."""

from app.models.document import Document, IngestionJob, OutboxEvent
from app.models.mfa import MfaRecoveryCode, UserMfa
from app.models.one_time_token import OneTimeToken
from app.models.refresh_token import RefreshToken
from app.models.user import User

__all__ = [
    "Document",
    "IngestionJob",
    "MfaRecoveryCode",
    "OneTimeToken",
    "OutboxEvent",
    "RefreshToken",
    "User",
    "UserMfa",
]
