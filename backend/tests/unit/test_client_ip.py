"""Which address the per-IP limits use (frontend forwarding with a shared secret)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import SecretStr
from starlette.requests import Request

from app.dependencies.auth import client_ip

SECRET = "f" * 40


def request(headers: dict[str, str], secret: str = SECRET) -> Request:
    settings = SimpleNamespace(frontend_client_ip_secret=SecretStr(secret))
    app = SimpleNamespace(state=SimpleNamespace(infra=SimpleNamespace(settings=settings)))
    scope = {
        "type": "http",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "client": ("10.0.0.5", 1234),
        "app": app,
    }
    return Request(scope)


def test_forwarded_address_is_used_with_the_right_secret() -> None:
    req = request({"X-Frontend-Secret": SECRET, "X-End-User-IP": "203.0.113.7"})
    assert client_ip(req) == "203.0.113.7"


def test_ipv6_is_normalised() -> None:
    req = request({"X-Frontend-Secret": SECRET, "X-End-User-IP": "2001:DB8::0001"})
    assert client_ip(req) == "2001:db8::1"


@pytest.mark.parametrize(
    "headers",
    [
        {"X-End-User-IP": "203.0.113.7"},  # no secret
        {"X-Frontend-Secret": "wrong" * 8, "X-End-User-IP": "203.0.113.7"},
        {"X-Frontend-Secret": SECRET, "X-End-User-IP": "not-an-ip"},
        {"X-Frontend-Secret": SECRET, "X-End-User-IP": "203.0.113.7, 10.0.0.1"},
        {"X-Frontend-Secret": SECRET},  # no address
    ],
)
def test_anything_unproven_falls_back_to_the_connection(headers: dict[str, str]) -> None:
    assert client_ip(request(headers)) == "10.0.0.5"


def test_forwarding_is_off_without_a_configured_secret() -> None:
    req = request({"X-Frontend-Secret": "", "X-End-User-IP": "203.0.113.7"}, secret="")
    assert client_ip(req) == "10.0.0.5"
