"""Deployment templates must produce settings the application accepts (no Docker needed).

Catches drift such as a production rule that a template cannot satisfy: the single-box compose
file, the backend env template and the free-tier variant are all validated through `Settings`.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, cast

import yaml

from app.core.config import Settings

ROOT = Path(__file__).resolve().parents[3]
_VAR = re.compile(r"\$\{([A-Z0-9_]+)(?::?[-?][^}]*)?\}")

PLACEHOLDERS = {
    "SITE_DOMAIN": "rag.example.org",
    "COMPOSE_POSTGRES_PASSWORD": "a-long-unique-db-password",
    "JWT_SECRET": "j" * 48,
    "MFA_ENCRYPTION_KEY": "dGVzdC1vbmx5LWZlcm5ldC1rZXktMzItYnl0ZXMhISE=",
    "FRONTEND_CLIENT_IP_SECRET": "f" * 40,
    "EMAIL_FROM": "rag@example.org",
    "SMTP_HOST": "smtp.example.org",
    "SMTP_USERNAME": "user",
    "SMTP_PASSWORD": "password",
    "LLM_BASE_URL": "https://llm.example.org/v1",
    "LLM_API_KEY": "placeholder",
    "LLM_MODEL": "some-model",
}


class _Loader(yaml.SafeLoader):
    """SafeLoader that understands compose's `!reset` tag."""


_Loader.add_constructor("!reset", lambda loader, node: None)


def _resolve(value: Any) -> str:
    def repl(m: re.Match[str]) -> str:
        name = m.group(1)
        default = re.match(r"\$\{[A-Z0-9_]+:-([^}]*)\}", m.group(0))
        return PLACEHOLDERS.get(name, default.group(1) if default else "")

    return _VAR.sub(repl, str(value))


def _compose_env(*files: str, service: str) -> dict[str, str]:
    env: dict[str, str] = {}
    for name in files:
        data: dict[str, Any] = yaml.load((ROOT / name).read_text(encoding="utf-8"), Loader=_Loader)  # noqa: S506
        raw = cast(dict[str, Any], data["services"][service].get("environment") or {})
        env.update({str(k): _resolve(v) for k, v in raw.items()})
    return env


def _settings_from(env: dict[str, str]) -> Settings:
    fields = {k.lower(): v for k, v in env.items() if k.lower() in Settings.model_fields}
    return Settings.build(**cast(dict[str, Any], fields))


def test_single_box_compose_is_a_valid_production_configuration() -> None:
    files = ("docker-compose.yml", "deploy/compose.single-box.yml")
    for service in ("migrate", "api", "worker", "beat"):
        settings = _settings_from(_compose_env(*files, service=service))
        assert settings.app_env == "prod", service
        assert settings.postgres_host == "postgres"
        assert settings.require_verified_email


def test_backend_env_template_is_a_valid_production_configuration() -> None:
    env: dict[str, str] = {}
    for line in (ROOT / "deploy" / "backend.env.example").read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            env[key] = value.replace(
                "REPLACE_WITH_AT_LEAST_32_RANDOM_CHARACTERS", "r" * 40
            ).replace("REPLACE_WITH_A_FERNET_KEY", PLACEHOLDERS["MFA_ENCRYPTION_KEY"])
    settings = _settings_from(env)
    assert settings.app_env == "prod" and settings.postgres_sslmode == "verify-full"


def test_free_tier_env_template_is_a_valid_free_production_configuration() -> None:
    env: dict[str, str] = {}
    for line in (
        (ROOT / "deploy" / "free" / "backend.env.example").read_text(encoding="utf-8").splitlines()
    ):
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            env[key] = (
                value.replace("REPLACE_WITH_AT_LEAST_32_RANDOM_CHARACTERS", "r" * 40)
                .replace("REPLACE_WITH_A_FERNET_KEY", PLACEHOLDERS["MFA_ENCRYPTION_KEY"])
                .replace("REPLACE_WITH_AN_INVITE_CODE_OF_12_OR_MORE_CHARACTERS", "invite-code-123")
            )
    settings = _settings_from(env)
    assert settings.deployment_profile == "free" and settings.task_runner == "inline"
    assert settings.embedding_backend == "hosted" and not settings.rerank_enabled
    assert settings.postgres_sslmode == "verify-full" and not settings.ocr_enabled
