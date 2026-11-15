"""Free-tier variant through the real API: invite-only sign-up (ADR-042)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from pydantic import SecretStr

from app.core.config import get_settings
from tests.integration.docs_helpers import API

pytestmark = pytest.mark.integration

CODE = "an-invite-code-for-tests"


async def test_sign_up_needs_the_invite_code_when_one_is_configured(
    client_with_settings: Any,
) -> None:
    api = await client_with_settings(
        get_settings().model_copy(update={"registration_invite_code": SecretStr(CODE)})
    )

    def body(**extra: str) -> dict[str, str]:
        return {
            "email": f"u-{uuid.uuid4().hex[:10]}@example.com",
            "password": "a-Long-pass-phrase-77",
            **extra,
        }

    missing = await api.post(f"{API}/auth/register", json=body())
    wrong = await api.post(f"{API}/auth/register", json=body(invite_code="not-the-code"))
    right = await api.post(f"{API}/auth/register", json=body(invite_code=CODE))
    assert missing.status_code == wrong.status_code == 403
    assert missing.json()["error"]["code"] == "INVITE_REQUIRED"
    assert right.status_code == 201


async def test_without_a_configured_code_sign_up_is_unchanged(client: Any) -> None:
    ok = await client.post(
        f"{API}/auth/register",
        json={
            "email": f"u-{uuid.uuid4().hex[:10]}@example.com",
            "password": "a-Long-pass-phrase-77",
        },
    )
    assert ok.status_code == 201
