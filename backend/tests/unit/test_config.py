from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from app.core.config import Settings


def make(**overrides: Any) -> Settings:
    overrides.setdefault("jwt_secret", "x" * 40)
    overrides.setdefault("mfa_encryption_key", "dGVzdC1vbmx5LWZlcm5ldC1rZXktMzItYnl0ZXMhISE=")
    if overrides.get("app_env") == "prod":
        overrides.setdefault("postgres_sslmode", "verify-full")
    return Settings.build(**overrides)


def test_defaults_are_dev_safe() -> None:
    s = make(app_env="dev")
    assert s.app_env == "dev"
    assert s.postgres_host == "127.0.0.1"


def test_secret_not_in_repr() -> None:
    s = make(app_env="dev", postgres_password="hunter2-very-secret")
    assert "hunter2-very-secret" not in repr(s)
    assert "hunter2-very-secret" not in str(s.model_dump())


def test_database_url_escapes_special_characters() -> None:
    s = make(app_env="dev", postgres_password="p@ss:w/rd%#?")
    url = s.database_url(driver="asyncpg")
    assert url.password == "p@ss:w/rd%#?"
    rendered = url.render_as_string(hide_password=False)
    assert "p@ss:w/rd" not in rendered  # special characters are percent-encoded
    assert "***" in url.render_as_string(hide_password=True)


@pytest.mark.parametrize("pw", ["", "changeme", "Password", "postgres"])
def test_prod_rejects_default_passwords(pw: str) -> None:
    with pytest.raises(ValidationError):
        make(app_env="prod", postgres_password=pw, log_json=True)


def test_prod_requires_json_logs() -> None:
    with pytest.raises(ValidationError):
        make(app_env="prod", postgres_password="a-long-unique-secret", log_json=False)


def test_prod_ok_with_strong_secret() -> None:
    s = make(
        app_env="prod",
        postgres_sslmode="verify-full",
        postgres_password="a-long-unique-secret",
        log_json=True,
        argon2_memory_kib=65536,
        argon2_time_cost=3,
        email_backend="smtp",
        smtp_host="smtp.example.com",
        require_verified_email=True,
    )
    assert s.app_env == "prod"


def test_test_env_requires_test_database() -> None:
    with pytest.raises(ValidationError):
        make(app_env="test", postgres_db="rag_dev")
    assert make(app_env="test", postgres_db="rag_test").postgres_db == "rag_test"


def test_celery_soft_limit_must_be_below_hard_limit() -> None:
    with pytest.raises(ValidationError):
        make(app_env="dev", celery_task_time_limit_s=60, celery_task_soft_time_limit_s=60)


def test_collection_prefix() -> None:
    assert make(app_env="dev", qdrant_collection_prefix="test_").collection_name("chunks") == (
        "test_chunks"
    )


@pytest.mark.parametrize("secret", ["", "short", "x" * 31])
def test_jwt_secret_required_and_long_enough(secret: str) -> None:
    with pytest.raises(ValidationError):
        make(jwt_secret=secret)


def test_prod_rejects_weak_argon2_parameters() -> None:
    with pytest.raises(ValidationError):
        make(
            app_env="prod",
            postgres_password="a-long-unique-secret",
            log_json=True,
            argon2_memory_kib=8192,
            argon2_time_cost=1,
        )


def test_jwt_secret_not_in_repr() -> None:
    assert "x" * 40 not in repr(make())


def test_prod_requires_smtp_email_backend() -> None:
    with pytest.raises(ValidationError):
        make(
            app_env="prod",
            postgres_password="a-long-unique-secret",
            log_json=True,
            argon2_memory_kib=65536,
            argon2_time_cost=3,
            require_verified_email=True,
        )


def test_retired_jwt_key_validation() -> None:
    ok = make(jwt_key_id="k2", jwt_retired_keys={"k1": "o" * 40})
    assert set(ok.jwt_retired_keys) == {"k1"}
    with pytest.raises(ValidationError):
        make(jwt_key_id="k1", jwt_retired_keys={"k1": "o" * 40})  # active kid cannot be retired
    with pytest.raises(ValidationError):
        make(jwt_key_id="k2", jwt_retired_keys={"k1": "short"})


def test_mfa_key_must_be_valid_fernet_key() -> None:
    for bad in ("", "not-base64!", "c2hvcnQ="):
        with pytest.raises(ValidationError):
            make(mfa_encryption_key=bad)


def test_smtp_ssl_and_starttls_are_exclusive() -> None:
    with pytest.raises(ValidationError):
        make(smtp_use_ssl=True, smtp_starttls=True)


def test_prod_rejects_unencrypted_database_connections() -> None:
    with pytest.raises(ValidationError, match="postgres_sslmode=verify-full"):
        make(
            app_env="prod",
            postgres_sslmode="disable",
            postgres_password="a-long-unique-secret",
            log_json=True,
            email_backend="smtp",
            smtp_host="smtp.example.com",
            require_verified_email=True,
        )


@pytest.mark.parametrize("schema", ["rag;DROP TABLE users", "rag,public", "bad schema", "1rag"])
def test_schema_cannot_inject_connection_options(schema: str) -> None:
    with pytest.raises(ValidationError):
        make(postgres_schema=schema)
