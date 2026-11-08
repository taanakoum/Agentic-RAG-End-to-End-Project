"""Refresh token persistence."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.refresh_token import RefreshToken


@dataclass(frozen=True, slots=True)
class SessionRow:
    family_id: uuid.UUID
    created_at: datetime
    last_used_at: datetime
    user_agent: str | None


class RefreshTokenRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(
        self,
        *,
        user_id: uuid.UUID,
        family_id: uuid.UUID,
        token_hash: str,
        expires_at: datetime,
        user_agent: str | None,
    ) -> RefreshToken:
        token = RefreshToken(
            user_id=user_id,
            family_id=family_id,
            token_hash=token_hash,
            expires_at=expires_at,
            user_agent=user_agent[:256] if user_agent else None,
        )
        self._session.add(token)
        await self._session.flush()
        return token

    async def get_by_hash_for_update(self, token_hash: str) -> RefreshToken | None:
        """Row-locks the token so concurrent refreshes with the same token serialize."""
        result = await self._session.execute(
            select(RefreshToken).where(RefreshToken.token_hash == token_hash).with_for_update()
        )
        return result.scalar_one_or_none()

    async def revoke(self, token: RefreshToken, *, reason: str, now: datetime) -> None:
        token.revoked_at = now
        token.revoked_reason = reason
        await self._session.flush()

    async def revoke_family(self, family_id: uuid.UUID, *, reason: str, now: datetime) -> int:
        result = await self._session.execute(
            update(RefreshToken)
            .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=now, revoked_reason=reason)
        )
        return int(getattr(result, "rowcount", 0) or 0)

    async def family_is_active(self, family_id: uuid.UUID, *, now: datetime) -> bool:
        """True while the family has an unrevoked, unexpired token (the session is alive)."""
        result = await self._session.execute(
            select(RefreshToken.id)
            .where(
                RefreshToken.family_id == family_id,
                RefreshToken.revoked_at.is_(None),
                RefreshToken.expires_at > now,
            )
            .limit(1)
        )
        return result.first() is not None

    async def family_owner(self, family_id: uuid.UUID) -> uuid.UUID | None:
        """The user owning a session family, or None if it does not exist."""
        result = await self._session.execute(
            select(RefreshToken.user_id).where(RefreshToken.family_id == family_id).limit(1)
        )
        return result.scalar_one_or_none()

    async def list_active_sessions(self, user_id: uuid.UUID, *, now: datetime) -> list[SessionRow]:
        """The user's live sessions (families with an unrevoked, unexpired token), newest first."""
        rows = await self._session.execute(
            select(
                RefreshToken.family_id,
                func.min(RefreshToken.issued_at),
                func.max(RefreshToken.issued_at),
                func.max(RefreshToken.user_agent),
            )
            .where(RefreshToken.user_id == user_id)
            .group_by(RefreshToken.family_id)
            .having(
                func.bool_or((RefreshToken.revoked_at.is_(None)) & (RefreshToken.expires_at > now))
            )
            .order_by(func.max(RefreshToken.issued_at).desc())
        )
        return [SessionRow(r[0], r[1], r[2], r[3]) for r in rows.all()]

    async def revoke_all_for_user(
        self,
        user_id: uuid.UUID,
        *,
        reason: str,
        now: datetime,
        keep_family: uuid.UUID | None = None,
    ) -> int:
        """Revoke every live session of a user (optionally sparing one family)."""
        stmt = update(RefreshToken).where(
            RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None)
        )
        if keep_family is not None:
            stmt = stmt.where(RefreshToken.family_id != keep_family)
        result = await self._session.execute(stmt.values(revoked_at=now, revoked_reason=reason))
        return int(getattr(result, "rowcount", 0) or 0)
