"""Provider-neutral contracts for language-model calls.

Everything above this layer (the agent graph, services) depends only on these types, so the
hosted provider (Gemini, OpenRouter, Hugging Face, Groq, ...) is configuration, and tests use a
scripted model that never touches the network.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

Role = Literal["system", "user", "assistant"]
CacheStatus = Literal["hit", "miss", "bypass", "error"]
CacheScope = Literal["user", "global"]


@dataclass(frozen=True, slots=True)
class ChatMessage:
    role: Role
    content: str


@dataclass(frozen=True, slots=True)
class LLMRequest:
    messages: tuple[ChatMessage, ...]
    # a JSON schema the answer must follow (used natively where the provider supports it; the
    # caller validates the result either way)
    json_schema: dict[str, Any] | None = None
    schema_name: str = "response"
    schema_version: str = "1"
    temperature: float | None = None
    max_tokens: int | None = None
    # who owns the data inside the prompt. `global` is ONLY for prompts without any user data.
    cache_scope: CacheScope = "user"
    user_id: uuid.UUID | None = None
    use_cache: bool = True


@dataclass(slots=True)
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass(slots=True)
class LLMResponse:
    text: str
    model: str
    usage: Usage = field(default_factory=Usage)
    latency_ms: float = 0.0
    attempts: int = 1
    cache: CacheStatus = "bypass"
    # set when the response may be stored AFTER the caller validated it (see LLMGateway.remember)
    cache_key: str | None = None
    fallback_used: bool = False


class ChatModel(Protocol):
    """One hosted (or scripted) model endpoint."""

    @property
    def model_name(self) -> str: ...

    async def complete(self, request: LLMRequest) -> LLMResponse: ...
