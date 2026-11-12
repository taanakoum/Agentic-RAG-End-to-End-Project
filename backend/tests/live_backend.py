"""The real application on a real socket in its own thread and event loop.

Needed by tests of synchronous clients (the Streamlit frontend's httpx client), which cannot share
the test's event loop. The server runs the production lifespan (its own Infrastructure); only the
language model is replaced, by a deterministic scripted model.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

import uvicorn

from app.core.config import Settings, get_settings
from app.llm.cache import LLMGateway
from app.main import create_app
from tests.fakes import ScriptedModel


class LiveBackend:
    def __init__(self, model: ScriptedModel, settings: Settings | None = None) -> None:
        self.model = model
        self.settings = settings or get_settings()
        self.app = create_app(self.settings)
        self._server = uvicorn.Server(
            uvicorn.Config(self.app, host="127.0.0.1", port=0, log_level="warning", lifespan="on")
        )
        self._thread = threading.Thread(target=self._run, daemon=True, name="live-backend")
        self._error: BaseException | None = None

    def _run(self) -> None:
        try:
            asyncio.run(self._server.serve())
        except BaseException as exc:  # reported by start()
            self._error = exc

    def start(self) -> LiveBackend:
        self._thread.start()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self._error is not None:
                raise self._error
            infra: Any = getattr(self.app.state, "infra", None)
            if self._server.started and infra is not None:
                infra.llm = LLMGateway(self.model, infra.redis, self.settings)
                return self
            time.sleep(0.05)
        raise TimeoutError("the live backend did not start")

    @property
    def api_url(self) -> str:
        port = self._server.servers[0].sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}/api/v1"

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=20)
