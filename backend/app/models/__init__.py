"""Import all model modules so Alembic autogenerate and metadata see them."""

from app.models.refresh_token import RefreshToken
from app.models.user import User

__all__ = ["RefreshToken", "User"]
