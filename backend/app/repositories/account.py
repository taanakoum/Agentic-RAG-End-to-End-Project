"""Persistence for one-time tokens and MFA state."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.mfa import MfaRecoveryCode, UserMfa
from app.models.one_time_token import OneTimeToken


class OneTimeTokenRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(
        self, *, user_id: uuid.UUID, purpose: str, token_hash: str, expires_at: datetime
    ) -> None:
        self._session.add(
            OneTimeToken(
                user_id=user_id, purpose=purpose, token_hash=token_hash, expires_at=expires_at
            )
        )
        await self._session.flush()

    async def invalidate_unused(self, user_id: uuid.UUID, purpose: str, *, now: datetime) -> None:
        await self._session.execute(
            update(OneTimeToken)
            .where(
                OneTimeToken.user_id == user_id,
                OneTimeToken.purpose == purpose,
                OneTimeToken.used_at.is_(None),
            )
            .values(used_at=now)
        )

    async def consume(self, token_hash: str, purpose: str, *, now: datetime) -> uuid.UUID | None:
        """Atomically mark an unused, unexpired token as used and return its user (else None).

        The UPDATE takes a row lock held until the surrounding transaction ends, so two
        concurrent consumers cannot both succeed.
        """
        result = await self._session.execute(
            update(OneTimeToken)
            .where(
                OneTimeToken.token_hash == token_hash,
                OneTimeToken.purpose == purpose,
                OneTimeToken.used_at.is_(None),
                OneTimeToken.expires_at > now,
            )
            .values(used_at=now)
            .returning(OneTimeToken.user_id)
        )
        return result.scalar_one_or_none()


class MfaRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, user_id: uuid.UUID) -> UserMfa | None:
        return await self._session.get(UserMfa, user_id)

    async def upsert_unconfirmed(self, user_id: uuid.UUID, secret_encrypted: str) -> None:
        existing = await self.get(user_id)
        if existing is None:
            self._session.add(UserMfa(user_id=user_id, secret_encrypted=secret_encrypted))
        else:
            existing.secret_encrypted = secret_encrypted
            existing.confirmed_at = None
            existing.last_used_step = None
        await self._session.flush()

    async def advance_step(self, user_id: uuid.UUID, step: int) -> bool:
        """Record a used TOTP step; False if this step (or a later one) was already used."""
        result = await self._session.execute(
            update(UserMfa)
            .where(
                UserMfa.user_id == user_id,
                (UserMfa.last_used_step.is_(None)) | (UserMfa.last_used_step < step),
            )
            .values(last_used_step=step)
            .returning(UserMfa.user_id)
        )
        return result.scalar_one_or_none() is not None

    async def confirm(self, user_id: uuid.UUID, *, now: datetime) -> None:
        await self._session.execute(
            update(UserMfa).where(UserMfa.user_id == user_id).values(confirmed_at=now)
        )

    async def replace_recovery_codes(self, user_id: uuid.UUID, hashes: list[str]) -> None:
        await self._session.execute(
            delete(MfaRecoveryCode).where(MfaRecoveryCode.user_id == user_id)
        )
        self._session.add_all(MfaRecoveryCode(user_id=user_id, code_hash=h) for h in hashes)
        await self._session.flush()

    async def consume_recovery_code(
        self, user_id: uuid.UUID, code_hash: str, *, now: datetime
    ) -> bool:
        result = await self._session.execute(
            update(MfaRecoveryCode)
            .where(
                MfaRecoveryCode.user_id == user_id,
                MfaRecoveryCode.code_hash == code_hash,
                MfaRecoveryCode.used_at.is_(None),
            )
            .values(used_at=now)
            .returning(MfaRecoveryCode.id)
        )
        return result.scalar_one_or_none() is not None

    async def remaining_recovery_codes(self, user_id: uuid.UUID) -> int:
        result = await self._session.execute(
            select(MfaRecoveryCode.id).where(
                MfaRecoveryCode.user_id == user_id, MfaRecoveryCode.used_at.is_(None)
            )
        )
        return len(result.all())

    async def delete_all(self, user_id: uuid.UUID) -> None:
        await self._session.execute(
            delete(MfaRecoveryCode).where(MfaRecoveryCode.user_id == user_id)
        )
        await self._session.execute(delete(UserMfa).where(UserMfa.user_id == user_id))
