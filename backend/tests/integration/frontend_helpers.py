"""Fixtures and helpers for tests of the Streamlit frontend against the live backend."""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, text
from streamlit.testing.v1 import AppTest

from app.core.config import get_settings
from app.ingestion.embedding import set_embedder_factory
from app.ingestion.runtime import WorkerRuntime
from app.retrieval.rerank import set_reranker_factory
from frontend.api import ApiClient
from frontend.config import FrontendConfig
from tests.fakes import HashEmbedder, KeywordReranker, ScriptedModel
from tests.integration.agent_helpers import BUDGET_TEXT, GOOD, SESSION_TEXT
from tests.live_backend import LiveBackend
from tests.pdfs import make_pdf

APP = Path(__file__).resolve().parents[3] / "frontend" / "app.py"
PASSWORD = "Correct-Horse-Battery-9!"


@pytest.fixture(scope="module")
def model() -> ScriptedModel:
    return ScriptedModel(GOOD)


@pytest.fixture(scope="module")
def backend(runtime: WorkerRuntime, model: ScriptedModel) -> Iterator[LiveBackend]:
    """The real API in a thread + the real Celery worker (`runtime`); only the model is scripted."""
    set_embedder_factory(HashEmbedder)
    set_reranker_factory(KeywordReranker)
    live = LiveBackend(model).start()
    yield live
    live.stop()
    set_embedder_factory(None)
    set_reranker_factory(None)


@pytest.fixture(autouse=True)
def reset_model(model: ScriptedModel) -> None:
    model.script = [GOOD]
    model.calls.clear()
    model.delay_s = 0.0


def config_for(live: LiveBackend) -> FrontendConfig:
    return FrontendConfig(api_base_url=live.api_url, poll_interval_s=1)


def new_client(live: LiveBackend) -> ApiClient:
    return ApiClient(config_for(live))


def signed_in(live: LiveBackend, email: str) -> ApiClient:
    client = new_client(live)
    client.register(email, PASSWORD)
    client.login(email, PASSWORD)
    return client


def wait_completed(client: ApiClient, document_id: str, wait_s: float = 90) -> dict[str, Any]:
    deadline = time.monotonic() + wait_s
    document: dict[str, Any] = {}
    while time.monotonic() < deadline:
        document = client.get_document(document_id)
        if document["status"] in {"completed", "failed"}:
            return document
        time.sleep(0.25)
    raise AssertionError(f"document stuck: {document.get('status')}")


def handbook_pdf() -> bytes:
    """Two pages holding the two sentences the scripted answer cites."""
    return make_pdf([BUDGET_TEXT, SESSION_TEXT])


def upload_handbook(client: ApiClient, name: str = "handbook.pdf") -> dict[str, Any]:
    uploaded = client.upload(name, handbook_pdf(), "application/pdf")
    return wait_completed(client, uploaded["document"]["id"])


def app_for(live: LiveBackend, client: ApiClient | None = None) -> AppTest:
    """The Streamlit app in-process (AppTest), optionally already signed in as `client`."""
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.session_state["config"] = config_for(live)
    if client is not None:
        at.session_state["api"] = client
        at.session_state["user"] = client.me()
    return at


def db_scalar(sql: str, **params: Any) -> Any:
    engine = create_engine(get_settings().database_url(driver="psycopg"))
    try:
        with engine.connect() as connection:
            return connection.execute(text(sql), params).scalar()
    finally:
        engine.dispose()


def button(at: AppTest, label: str) -> Any:
    matches = [b for b in at.button if b.label == label]
    assert matches, f"no button {label!r}; have {[b.label for b in at.button]}"
    return matches[0]


def text_of(at: AppTest) -> str:
    """Everything a person would read on the page."""
    parts: list[str] = []
    for group in (
        at.markdown,
        at.caption,
        at.error,
        at.warning,
        at.info,
        at.success,
        at.title,
        at.subheader,
    ):
        parts.extend(str(element.value) for element in group)
    return "\n".join(parts)
