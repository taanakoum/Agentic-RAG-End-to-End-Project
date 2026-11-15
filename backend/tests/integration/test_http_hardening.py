"""Response headers and CORS: defensive by default, never overriding what an endpoint sets."""

from __future__ import annotations

from typing import Any

import httpx2
import pytest
from pydantic import SecretStr

from app.core.config import get_settings
from tests.integration.agent_helpers import make_actor
from tests.integration.docs_helpers import API

pytestmark = pytest.mark.integration

BASELINE = {
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "x-frame-options": "DENY",
    "cache-control": "no-store",
    "content-security-policy": "default-src 'none'; frame-ancestors 'none'",
}


def assert_hardened(response: httpx2.Response, what: str) -> None:
    for name, value in BASELINE.items():
        assert response.headers.get(name) == value, f"{what}: {name} = {response.headers.get(name)}"
    assert "strict-transport-security" not in response.headers  # development is plain HTTP


async def test_every_kind_of_response_carries_the_defensive_headers(
    client: httpx2.AsyncClient,
) -> None:
    alice = await make_actor(client)
    responses = {
        "200 health": await client.get(f"{API}/health/live"),
        "200 authed": await client.get(f"{API}/auth/me", headers=alice.headers),
        "401": await client.get(f"{API}/auth/me"),
        "404": await client.get(
            f"{API}/sessions/00000000-0000-4000-8000-000000000000", headers=alice.headers
        ),
        "405": await client.put(f"{API}/auth/me", headers=alice.headers),
        "422": await client.post(f"{API}/auth/login", json={"email": "x"}),
        "login": await client.post(
            f"{API}/auth/login", json={"email": "alice@example.com", "password": "wrong-password"}
        ),
    }
    for what, response in responses.items():
        assert_hardened(response, what)
    assert responses["401"].status_code == 401 and responses["405"].status_code == 405


async def test_a_413_from_the_body_limit_is_hardened_too(client: httpx2.AsyncClient) -> None:
    too_big = await client.post(
        f"{API}/auth/login",
        content=b"x" * (get_settings().max_body_bytes + 10),
        headers={"content-type": "application/json"},
    )
    assert too_big.status_code == 413
    assert_hardened(too_big, "413")


async def test_the_interactive_docs_keep_working_but_everything_else_stays_locked_down(
    client: httpx2.AsyncClient,
) -> None:
    docs = await client.get("/docs")
    assert docs.status_code == 200
    assert docs.headers["x-content-type-options"] == "nosniff"
    assert "content-security-policy" not in docs.headers  # Swagger UI needs scripts (dev only)


async def test_an_endpoints_own_cache_header_is_not_overridden(
    client: httpx2.AsyncClient, client_with_settings: Any
) -> None:
    alice = await make_actor(client)
    sse = await client.post(
        f"{API}/chat/stream",
        json={"session_id": "00000000-0000-4000-8000-000000000000", "message": "hi"},
        headers={**alice.headers, "Idempotency-Key": "hardening-key-1"},
    )
    assert sse.status_code == 404  # unknown session: still a hardened JSON error
    assert_hardened(sse, "stream refusal")


async def test_hsts_is_sent_only_for_the_production_environment(
    client_with_settings: Any,
) -> None:

    base = get_settings()
    prod = base.model_copy(update={"app_env": "prod"})
    client = await client_with_settings(prod)
    live = await client.get(f"{API}/health/live")
    assert live.headers["strict-transport-security"] == "max-age=63072000; includeSubDomains"
    assert (await client.get("/docs")).status_code == 404  # docs are off in production


async def test_cors_is_off_by_default_and_exact_when_configured(
    client: httpx2.AsyncClient, client_with_settings: Any
) -> None:
    preflight = {
        "Origin": "https://evil.example",
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "authorization,content-type,idempotency-key,x-request-id",
    }
    off = await client.options(f"{API}/auth/login", headers=preflight)
    assert "access-control-allow-origin" not in off.headers

    configured = get_settings().model_copy(update={"cors_origins": ["https://app.example"]})
    cors = await client_with_settings(configured)
    allowed = await cors.options(
        f"{API}/auth/login", headers={**preflight, "Origin": "https://app.example"}
    )
    assert allowed.headers["access-control-allow-origin"] == "https://app.example"
    assert allowed.headers["access-control-allow-origin"] != "*"
    assert allowed.status_code == 200
    denied = await cors.options(f"{API}/auth/login", headers=preflight)
    assert "access-control-allow-origin" not in denied.headers
    simple = await cors.get(f"{API}/health/live", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in simple.headers
    visible = await cors.get(f"{API}/health/live", headers={"Origin": "https://app.example"})
    assert "X-Request-ID" in visible.headers["access-control-expose-headers"]
    assert "Retry-After" in visible.headers["access-control-expose-headers"]


async def test_per_ip_limits_see_the_end_user_only_when_the_frontend_proves_itself(
    client_with_settings: Any,
) -> None:
    """Behind the Streamlit server every request has the same address; the frontend forwards the
    user's address with a shared secret so one user cannot exhaust the limit for everybody."""
    import secrets
    import uuid

    secret = "s" * 40
    configured = get_settings().model_copy(
        update={
            "frontend_client_ip_secret": SecretStr(secret),
            "rl_login_ip_limit": 2,
        }
    )
    api = await client_with_settings(configured)
    a, b, c = (f"198.18.{secrets.randbelow(256)}.{1 + secrets.randbelow(254)}" for _ in range(3))

    async def login(ip: str, presented: str = secret) -> int:
        body = {"email": f"nobody-{uuid.uuid4().hex}@example.com", "password": "x" * 14}
        headers = {"X-Frontend-Secret": presented, "X-End-User-IP": ip}
        return (await api.post(f"{API}/auth/login", json=body, headers=headers)).status_code

    assert [await login(a), await login(a)] == [401, 401]
    assert await login(a) == 429  # user A is limited ...
    assert await login(b) == 401  # ... user B is not
    # A wrong secret is ignored: the claimed address does not count, the connection's does.
    assert [await login(c, "wrong" * 8), await login(c, "wrong" * 8)] == [401, 401]
    assert await login(c) == 401  # C's own bucket was never touched
