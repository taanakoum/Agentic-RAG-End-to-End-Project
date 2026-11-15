"""The Streamlit client forwards the end user's address only together with the shared secret."""

from __future__ import annotations

import httpx

from frontend.api import ApiClient
from frontend.config import FrontendConfig


def capture(config: FrontendConfig, ip: str | None) -> httpx.Headers:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json={"status": "ok"})

    client = ApiClient(config, end_user_ip=lambda: ip)
    client._http = httpx.Client(  # pyright: ignore[reportPrivateUsage]
        base_url=config.api_base_url, transport=httpx.MockTransport(handler)
    )
    client.request("GET", "/health/live", auth=False)
    return seen[0].headers


def test_address_and_secret_are_sent_together() -> None:
    headers = capture(FrontendConfig(client_ip_secret="k" * 40), "203.0.113.9")
    assert headers["x-end-user-ip"] == "203.0.113.9"
    assert headers["x-frontend-secret"] == "k" * 40


def test_nothing_is_sent_without_a_secret_or_an_address() -> None:
    for config, ip in [
        (FrontendConfig(), "203.0.113.9"),
        (FrontendConfig(client_ip_secret="k" * 40), None),
    ]:
        headers = capture(config, ip)
        assert "x-end-user-ip" not in headers and "x-frontend-secret" not in headers
