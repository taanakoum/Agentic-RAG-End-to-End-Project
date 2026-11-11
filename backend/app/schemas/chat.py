"""Session (conversation) and message API contracts."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AgentConfig(BaseModel):
    """The only agent options a user may choose (safe knobs; no prompts, tools or model keys)."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["single"] = "single"  # multi-agent was removed (ADR-028)
    top_k: int | None = Field(default=None, ge=1, le=30)
    rerank: bool | None = None


def _clean_title(value: str) -> str:
    cleaned = " ".join(value.split())
    if not cleaned:
        raise ValueError("title must not be blank")
    return cleaned


class ConversationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=200)
    agent_config: AgentConfig = Field(default_factory=AgentConfig)

    @field_validator("title")
    @classmethod
    def _title(cls, value: str | None) -> str | None:
        return None if value is None else _clean_title(value)


class ConversationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=200)
    agent_config: AgentConfig | None = None

    @field_validator("title")
    @classmethod
    def _title(cls, value: str | None) -> str | None:
        return None if value is None else _clean_title(value)


class ConversationPublic(BaseModel):
    id: uuid.UUID
    title: str
    agent_config: AgentConfig
    message_count: int
    created_at: datetime
    updated_at: datetime


class ConversationList(BaseModel):
    items: list[ConversationPublic]
    total: int
    limit: int
    offset: int


class MessagePublic(BaseModel):
    id: uuid.UUID
    seq: int
    role: Literal["user", "assistant", "system"]
    content: str
    response: dict[str, Any] | None = Field(
        default=None, description="The validated RAGResponse (assistant messages)"
    )
    execution_id: uuid.UUID | None
    created_at: datetime


class MessageList(BaseModel):
    items: list[MessagePublic]
    has_more: bool = Field(description="True if more messages exist after the last item")


class DeleteConversationAccepted(BaseModel):
    conversation_id: uuid.UUID
    status: Literal["deleting"] = "deleting"
