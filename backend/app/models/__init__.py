"""Import all model modules so Alembic autogenerate and metadata see them."""

from app.models.chat import ChatExecution, Conversation, Message
from app.models.document import Document, IngestionJob, OutboxEvent
from app.models.memory import Memory, UserPreferences
from app.models.mfa import MfaRecoveryCode, UserMfa
from app.models.one_time_token import OneTimeToken
from app.models.refresh_token import RefreshToken
from app.models.user import User

__all__ = [
    "ChatExecution",
    "Conversation",
    "Document",
    "IngestionJob",
    "Memory",
    "Message",
    "MfaRecoveryCode",
    "OneTimeToken",
    "OutboxEvent",
    "RefreshToken",
    "User",
    "UserMfa",
    "UserPreferences",
]
