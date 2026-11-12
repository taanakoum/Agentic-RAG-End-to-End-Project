"""What a user sees when a dependency is down: a stable 503, never a 500, a stack trace or a leak.

Each app below is the real application pointed at a port where nothing listens (the other two
dependencies stay healthy), called with a token from the healthy app.
"""

from __future__ import annotations

import socket
import uuid
from collections.abc import Awaitable, Callable, Iterator
from typing import Any

import httpx2
import pytest

from app.core.config import get_settings
from app.infra.container import Infrastructure
from tests.integration.agent_helpers import install_adapters, make_actor, seed_handbook
from tests.integration.docs_helpers import API

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _adapters() -> Iterator[None]:
    yield from install_adapters()


LEAKS = (
    "psycopg",
    "postgres",
    "sqlalchemy",
    "asyncpg",
    "qdrant",
    "redis",
    "traceback",
    "127.0.0.1",
)


def closed_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def assert_clean_503(response: httpx2.Response, what: str) -> None:
    assert response.status_code == 503, f"{what}: {response.status_code} {response.text[:200]}"
    body = response.json()["error"]
    assert body["code"] == "SERVICE_UNAVAILABLE" and body["request_id"], what
    lowered = response.text.lower()
    assert not [word for word in LEAKS if word in lowered], f"{what} leaked: {response.text[:300]}"
    assert response.headers["x-request-id"]


async def test_a_postgres_outage_is_a_503_on_every_kind_of_endpoint(
    client: httpx2.AsyncClient, client_with_settings: Any
) -> None:
    alice = await make_actor(client)
    down = await client_with_settings(
        get_settings().model_copy(update={"postgres_port": closed_port()})
    )
    calls: dict[str, Callable[[], Awaitable[httpx2.Response]]] = {
        "login": lambda: down.post(
            f"{API}/auth/login", json={"email": "alice@example.com", "password": "x" * 12}
        ),
        "register": lambda: down.post(
            f"{API}/auth/register",
            json={"email": "new@example.com", "password": "Correct-Horse-9!"},
        ),
        "me": lambda: down.get(f"{API}/auth/me", headers=alice.headers),
        "documents": lambda: down.get(f"{API}/documents", headers=alice.headers),
        "sessions": lambda: down.get(f"{API}/sessions", headers=alice.headers),
        "memory": lambda: down.get(f"{API}/memory", headers=alice.headers),
        "feedback": lambda: down.post(
            f"{API}/feedback",
            json={"message_id": str(uuid.uuid4()), "rating": "up"},
            headers=alice.headers,
        ),
    }
    for what, call in calls.items():
        assert_clean_503(await call(), what)
    ready = await down.get(f"{API}/health/ready")
    assert ready.status_code == 503 and ready.json()["components"]["postgres"]["status"] != "ok"
    live = await down.get(f"{API}/health/live")
    assert live.status_code == 200  # liveness never depends on a dependency


async def test_a_qdrant_outage_is_a_503_for_search_and_documents_stay_usable(
    client: httpx2.AsyncClient, client_with_settings: Any, infra: Infrastructure
) -> None:
    alice = await make_actor(client)
    await seed_handbook(infra, alice)  # search only touches Qdrant for users with documents
    down = await client_with_settings(
        get_settings().model_copy(update={"qdrant_url": f"http://127.0.0.1:{closed_port()}"})
    )
    search = await down.post(f"{API}/search", json={"query": "upload limit"}, headers=alice.headers)
    assert_clean_503(search, "search")
    # what does not need the vector store keeps working
    assert (await down.get(f"{API}/documents", headers=alice.headers)).status_code == 200
    assert (await down.get(f"{API}/sessions", headers=alice.headers)).status_code == 200
    ready = await down.get(f"{API}/health/ready")
    assert ready.status_code == 503 and ready.json()["components"]["qdrant"]["status"] != "ok"


async def test_a_redis_outage_is_a_503_not_a_500_and_does_not_open_the_rate_limiter(
    client: httpx2.AsyncClient, client_with_settings: Any
) -> None:
    alice = await make_actor(client)
    down = await client_with_settings(
        get_settings().model_copy(update={"redis_url": f"redis://127.0.0.1:{closed_port()}/0"})
    )
    login = await down.post(
        f"{API}/auth/login", json={"email": "alice@example.com", "password": "x" * 12}
    )
    assert_clean_503(login, "login")  # abuse limits fail closed
    session = await client.post(f"{API}/sessions", json={}, headers=alice.headers)
    chat = await down.post(
        f"{API}/chat",
        json={"session_id": session.json()["id"], "message": "hello"},
        headers={**alice.headers, "Idempotency-Key": "outage-key-0001"},
    )
    assert_clean_503(chat, "chat")  # the turn is refused, not half-run
