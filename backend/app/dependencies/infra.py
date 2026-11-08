from __future__ import annotations

from collections.abc import AsyncIterator
from typing import cast

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.infra.container import Infrastructure


def get_infra(request: Request) -> Infrastructure:
    return cast(Infrastructure, request.app.state.infra)


async def get_db_session(request: Request) -> AsyncIterator[AsyncSession]:
    """One session per request. Services own commit/rollback boundaries."""
    infra = get_infra(request)
    async with infra.session_factory() as session:
        yield session
