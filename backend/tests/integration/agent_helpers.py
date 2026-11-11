"""Helpers for agent / chat tests: scripted model, seeded documents, ready-made answers."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import httpx2
import pytest

from app.core.config import get_settings
from app.infra.container import Infrastructure
from app.ingestion.embedding import set_embedder_factory
from app.llm.cache import LLMGateway
from app.retrieval.rerank import set_reranker_factory
from app.services.auth_service import AuthenticatedUser
from tests.fakes import HashEmbedder, KeywordReranker, ScriptedModel
from tests.integration.docs_helpers import API, new_user
from tests.integration.retrieval_helpers import seed_document

BUDGET_TEXT = "Files larger than 25 megabytes are rejected by the gateway."
SESSION_TEXT = "Idle sessions are closed automatically after 30 minutes."


@dataclass(slots=True)
class Actor:
    user: AuthenticatedUser
    headers: dict[str, str]

    @property
    def id(self) -> uuid.UUID:
        return self.user.id


async def make_actor(client: httpx2.AsyncClient, email: str = "alice@example.com") -> Actor:
    headers = await new_user(client, email)
    me = await client.get(f"{API}/auth/me", headers=headers)
    user = AuthenticatedUser(
        id=uuid.UUID(me.json()["id"]), email=email, session_id=uuid.uuid4(), email_verified=True
    )
    return Actor(user, headers)


async def make_session(client: httpx2.AsyncClient, actor: Actor, **body: Any) -> uuid.UUID:
    resp = await client.post(f"{API}/sessions", json=body, headers=actor.headers)
    assert resp.status_code == 201, resp.text
    return uuid.UUID(resp.json()["id"])


async def seed_handbook(
    infra: Infrastructure, actor: Actor, name: str = "handbook.pdf"
) -> uuid.UUID:
    """Two chunks on pages 1 and 2 (page numbers come from the seeding helper)."""
    return await seed_document(infra, actor.id, name, [BUDGET_TEXT, SESSION_TEXT], section="Limits")


def answer_json(
    answer: str = "Files over 25 MB are rejected [S1].",
    *citations: tuple[str, str] | tuple[str, str, int],
    **flags: Any,
) -> str:
    body: dict[str, Any] = {
        "answer": answer,
        "citations": [
            {"source": c[0], "quote": c[1], **({"page": c[2]} if len(c) == 3 else {})}
            for c in citations
        ],
        "insufficient_evidence": False,
        "clarification_needed": False,
        "follow_up_questions": [],
        "support": "supported",
    }
    body.update(flags)
    return json.dumps(body)


GOOD = answer_json(
    "Files larger than 25 megabytes are rejected [S1].",
    ("S1", "larger than 25 megabytes are rejected"),
)


def install_adapters() -> Iterator[None]:
    set_embedder_factory(HashEmbedder)
    set_reranker_factory(KeywordReranker)
    yield
    set_embedder_factory(None)
    set_reranker_factory(None)


@pytest.fixture
def scripted(infra: Infrastructure) -> Iterator[ScriptedModel]:
    """Replace the process-wide LLM gateway with a scripted model for one test."""
    model = ScriptedModel(GOOD)
    original = infra.llm
    infra.llm = LLMGateway(model, infra.redis, get_settings())
    yield model
    infra.llm = original


def last_user_prompt(model: ScriptedModel, call: int = -1) -> str:
    return str(model.calls[call].messages[-1].content)


def cite_where(needle: str, quote: str, answer: str = "Found it [S{n}].") -> Any:
    """A scripted answer that cites whichever source label actually contains `needle`.

    Retrieval order depends on the question, so tests that care about WHICH chunk is cited use
    this instead of hard-coding "S1".
    """
    import re

    def build(request: Any) -> str:
        prompt = str(request.messages[-1].content)
        pattern = r'<source-\w+ id="S(\d+)"[^>]*>\n(.*?)\n</source-'
        for match in re.finditer(pattern, prompt, re.S):
            if needle in match.group(2):
                n = match.group(1)
                return answer_json(answer.format(n=n), (f"S{n}", quote))
        return answer_json("Not in the sources.", insufficient_evidence=True, support="not_found")

    return build
