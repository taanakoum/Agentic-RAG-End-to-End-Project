from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.routers import (
    account,
    auth,
    documents,
    health,
    ingestion,
    memory,
    mfa,
    search,
    sessions,
    users,
)

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(account.router)
api_router.include_router(mfa.router)
api_router.include_router(documents.router)
api_router.include_router(ingestion.router)
api_router.include_router(search.router)
api_router.include_router(sessions.router)
api_router.include_router(memory.router)
api_router.include_router(users.router)
