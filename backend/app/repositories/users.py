"""User persistence. All SQL for users lives here."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User


class UserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(
        self, *, email: str, email_normalized: str, password_hash: str, display_name: str | None
    ) -> User:
        """Insert and flush. A duplicate normalized email raises IntegrityError (the constraint)."""
        user = User(
            email=email,
            email_normalized=email_normalized,
            password_hash=password_hash,
            display_name=display_name,
        )
        self._session.add(user)
        await self._session.flush()
        return user

    async def get_by_id(self, user_id: uuid.UUID) -> User | None:
        return await self._session.get(User, user_id)

    async def get_by_email_normalized(self, email_normalized: str) -> User | None:
        result = await self._session.execute(
            select(User).where(User.email_normalized == email_normalized)
        )
        return result.scalar_one_or_none()
