"""User preferences (memory consent and retention)."""

from __future__ import annotations

from fastapi import APIRouter

from app.core.exceptions import error_responses
from app.dependencies.auth import CurrentUser, MemoryServiceDep
from app.schemas.memory import PreferencesPublic, PreferencesUpdate

router = APIRouter(prefix="/users/me", tags=["users"])


@router.get("/preferences", response_model=PreferencesPublic, responses=error_responses(401))
async def get_preferences(current: CurrentUser, service: MemoryServiceDep) -> PreferencesPublic:
    return await service.preferences(current)


@router.patch("/preferences", response_model=PreferencesPublic, responses=error_responses(401, 422))
async def update_preferences(
    body: PreferencesUpdate, current: CurrentUser, service: MemoryServiceDep
) -> PreferencesPublic:
    return await service.update_preferences(current, body)
