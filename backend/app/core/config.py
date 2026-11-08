"""Typed application settings (Pydantic Settings). All config comes from the environment / .env."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL

REPO_ROOT = Path(__file__).resolve().parents[3]

_INSECURE_PASSWORDS = frozenset({"", "changeme", "password", "postgres"})


class Settings(BaseSettings):
    """Runtime configuration. Dev, test and prod differ only by environment values."""

    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_env: Literal["dev", "test", "prod"] = "dev"
    service_name: str = "agentic-rag-api"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_json: bool = False

    # PostgreSQL
    postgres_user: str = "rag_app"
    postgres_password: SecretStr = SecretStr("")
    postgres_host: str = "127.0.0.1"
    postgres_port: int = Field(default=5432, ge=1, le=65535)
    postgres_db: str = "rag_dev"
    db_pool_size: int = Field(default=10, ge=1, le=100)
    db_max_overflow: int = Field(default=10, ge=0, le=100)
    db_pool_timeout_s: float = Field(default=30.0, gt=0)
    db_pool_recycle_s: int = Field(default=1800, ge=0)
    db_connect_timeout_s: int = Field(default=5, ge=1)

    # Redis (Celery broker, cache, rate limits). Never the only copy of critical data.
    redis_url: str = "redis://127.0.0.1:6379/0"
    redis_socket_timeout_s: float = Field(default=5.0, gt=0)

    # Qdrant
    qdrant_url: str = "http://127.0.0.1:6333"
    qdrant_api_key: SecretStr | None = None
    qdrant_timeout_s: float = Field(default=10.0, gt=0)
    qdrant_collection_prefix: str = ""

    # Object storage (local volume adapter; S3-compatible adapter arrives with deployment work)
    storage_backend: Literal["local"] = "local"
    storage_local_root: Path = REPO_ROOT / "data" / "objects"

    # Auth
    jwt_secret: SecretStr = SecretStr("")
    jwt_issuer: str = "agentic-rag"
    jwt_audience: str = "agentic-rag-api"
    access_token_ttl_s: int = Field(default=900, ge=60, le=3600)
    refresh_token_ttl_s: int = Field(default=14 * 24 * 3600, ge=300)
    password_min_length: int = Field(default=12, ge=8)
    password_max_length: int = Field(default=128, ge=32, le=1024)
    argon2_memory_kib: int = Field(default=65536, ge=1024)
    argon2_time_cost: int = Field(default=3, ge=1)
    argon2_parallelism: int = Field(default=4, ge=1, le=64)
    max_concurrent_hashes: int = Field(default=4, ge=1, le=64)

    # Rate limits (Redis). Fixed windows; limit = requests allowed per window.
    rl_login_ip_limit: int = Field(default=20, ge=1)
    rl_login_ip_window_s: int = Field(default=60, ge=1)
    rl_login_account_limit: int = Field(default=5, ge=1)
    rl_login_account_window_s: int = Field(default=900, ge=1)
    rl_register_ip_limit: int = Field(default=10, ge=1)
    rl_register_ip_window_s: int = Field(default=3600, ge=1)
    rl_refresh_ip_limit: int = Field(default=60, ge=1)
    rl_refresh_ip_window_s: int = Field(default=60, ge=1)

    # HTTP
    max_body_bytes: int = Field(default=1024 * 1024, ge=1024)
    cors_origins: list[str] = Field(default_factory=list)

    # Celery
    celery_task_time_limit_s: int = Field(default=900, ge=10)
    celery_task_soft_time_limit_s: int = Field(default=840, ge=5)
    celery_worker_concurrency: int = Field(default=2, ge=1, le=64)
    celery_health_timeout_s: float = Field(default=2.0, gt=0)

    @model_validator(mode="after")
    def _validate(self) -> Settings:
        if self.celery_task_soft_time_limit_s >= self.celery_task_time_limit_s:
            raise ValueError("celery_task_soft_time_limit_s must be < celery_task_time_limit_s")
        if len(self.jwt_secret.get_secret_value()) < 32:
            raise ValueError("jwt_secret must be set and at least 32 characters")
        if self.app_env == "prod":
            if self.argon2_memory_kib < 19456 or self.argon2_time_cost < 2:
                raise ValueError("argon2 parameters are below the OWASP minimum for prod")
            if self.postgres_password.get_secret_value().lower() in _INSECURE_PASSWORDS:
                raise ValueError("postgres_password must be set to a non-default value in prod")
            if not self.log_json:
                raise ValueError("log_json must be true in prod")
        if self.app_env == "test" and not self.postgres_db.endswith("_test"):
            raise ValueError("test environment must use a database whose name ends with _test")
        return self

    @classmethod
    def build(cls, *, env_file: Path | None = None, **overrides: Any) -> Settings:
        """Construct settings, optionally ignoring/overriding the .env file (used by tests)."""
        # pydantic-settings accepts `_env_file`; pyright's synthesized __init__ does not know it.
        return cls(_env_file=env_file, **overrides)  # pyright: ignore[reportCallIssue]

    def database_url(self, *, driver: Literal["asyncpg", "psycopg"]) -> URL:
        """SQLAlchemy URL object (keeps the password out of string formatting)."""
        return URL.create(
            drivername=f"postgresql+{driver}",
            username=self.postgres_user,
            password=self.postgres_password.get_secret_value(),
            host=self.postgres_host,
            port=self.postgres_port,
            database=self.postgres_db,
        )

    def collection_name(self, base: str) -> str:
        return f"{self.qdrant_collection_prefix}{base}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
