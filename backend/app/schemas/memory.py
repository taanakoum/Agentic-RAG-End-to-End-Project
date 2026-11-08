"""Memory and preference API contracts."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

MemoryKind = Literal["semantic", "episodic", "summary"]


def _clean(value: str) -> str:
    cleaned = " ".join(value.split())
    if not cleaned:
        raise ValueError("content must not be blank")
    return cleaned


class MemoryCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1, max_length=5000)
    kind: MemoryKind = "semantic"
    importance: int = Field(default=3, ge=1, le=5)

    @field_validator("content")
    @classmethod
    def _content(cls, value: str) -> str:
        return _clean(value)


class MemoryUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str | None = Field(default=None, min_length=1, max_length=5000)
    kind: MemoryKind | None = None
    importance: int | None = Field(default=None, ge=1, le=5)

    @field_validator("content")
    @classmethod
    def _content(cls, value: str | None) -> str | None:
        return None if value is None else _clean(value)


class MemoryPublic(BaseModel):
    id: uuid.UUID
    kind: MemoryKind
    content: str
    importance: int
    source: Literal["user", "consolidation"]
    created_at: datetime
    updated_at: datetime
    last_used_at: datetime | None


class MemoryList(BaseModel):
    items: list[MemoryPublic]
    total: int
    limit: int
    offset: int


class MemorySearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=1000)
    k: int | None = Field(default=None, ge=1, le=20)


class MemoryHitPublic(BaseModel):
    memory: MemoryPublic
    score: float


class MemorySearchResponse(BaseModel):
    results: list[MemoryHitPublic]


class MemoryResetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirm: Literal[True] = Field(description="Must be true: this deletes ALL memories")


class MemoryResetAccepted(BaseModel):
    status: Literal["resetting"] = "resetting"
    memories: int


class PreferencesPublic(BaseModel):
    memory_enabled: bool
    memory_retention_days: int


class PreferencesUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    memory_enabled: bool | None = None
    memory_retention_days: int | None = Field(default=None, ge=1, le=3650)
