"""Typed application settings (Pydantic Settings). All config comes from the environment / .env."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from cryptography.fernet import Fernet
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
    storage_backend: Literal["local", "s3"] = "local"
    s3_endpoint_url: str = ""  # empty = AWS; set for MinIO/R2/B2 etc.
    s3_bucket: str = ""
    s3_region: str = "us-east-1"
    s3_access_key: SecretStr = SecretStr("")
    s3_secret_key: SecretStr = SecretStr("")
    s3_path_style: bool = True
    s3_key_prefix: str = ""
    s3_timeout_s: int = Field(default=10, ge=1)
    storage_local_root: Path = REPO_ROOT / "data" / "objects"

    # Auth
    jwt_secret: SecretStr = SecretStr("")
    jwt_key_id: str = Field(default="k1", pattern=r"^[A-Za-z0-9._-]{1,32}$")
    # kid -> secret of retired signing keys: still accepted for verification, never used to sign.
    jwt_retired_keys: dict[str, SecretStr] = Field(default_factory=dict)
    jwt_issuer: str = "agentic-rag"
    jwt_audience: str = "agentic-rag-api"
    access_token_ttl_s: int = Field(default=900, ge=60, le=3600)
    refresh_token_ttl_s: int = Field(default=14 * 24 * 3600, ge=300)
    password_min_length: int = Field(default=12, ge=8)
    require_verified_email: bool = False
    password_max_length: int = Field(default=128, ge=32, le=1024)
    argon2_memory_kib: int = Field(default=65536, ge=1024)
    argon2_time_cost: int = Field(default=3, ge=1)
    argon2_parallelism: int = Field(default=4, ge=1, le=64)
    max_concurrent_hashes: int = Field(default=4, ge=1, le=64)
    thread_pool_size: int = Field(default=32, ge=4, le=256)

    # One-time tokens, email, MFA
    reset_token_ttl_s: int = Field(default=3600, ge=60)
    verify_token_ttl_s: int = Field(default=24 * 3600, ge=60)
    app_base_url: str = "http://localhost:8501"
    email_backend: Literal["file", "smtp"] = "file"
    email_from: str = "no-reply@agentic-rag.local"
    email_file_dir: Path = REPO_ROOT / "data" / "outbox"
    smtp_host: str = ""
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_username: str = ""
    smtp_password: SecretStr = SecretStr("")
    smtp_starttls: bool = True
    smtp_use_ssl: bool = False
    smtp_timeout_s: float = Field(default=10.0, gt=0)
    mfa_encryption_key: SecretStr = SecretStr("")
    mfa_issuer: str = "Agentic RAG"
    mfa_token_ttl_s: int = Field(default=300, ge=60, le=900)
    mfa_recovery_code_count: int = Field(default=10, ge=4, le=20)

    # Rate limits (Redis). Fixed windows; limit = requests allowed per window.
    rl_login_ip_limit: int = Field(default=20, ge=1)
    rl_login_ip_window_s: int = Field(default=60, ge=1)
    rl_login_account_limit: int = Field(default=5, ge=1)
    rl_login_account_window_s: int = Field(default=900, ge=1)
    rl_register_ip_limit: int = Field(default=10, ge=1)
    rl_register_ip_window_s: int = Field(default=3600, ge=1)
    rl_refresh_ip_limit: int = Field(default=60, ge=1)
    rl_refresh_ip_window_s: int = Field(default=60, ge=1)
    rl_forgot_ip_limit: int = Field(default=10, ge=1)
    rl_forgot_ip_window_s: int = Field(default=3600, ge=1)
    rl_forgot_account_limit: int = Field(default=3, ge=1)
    rl_forgot_account_window_s: int = Field(default=3600, ge=1)
    rl_reset_ip_limit: int = Field(default=10, ge=1)
    rl_reset_ip_window_s: int = Field(default=3600, ge=1)
    rl_mfa_limit: int = Field(default=5, ge=1)
    rl_mfa_window_s: int = Field(default=300, ge=1)
    rl_pwchange_limit: int = Field(default=5, ge=1)
    rl_pwchange_window_s: int = Field(default=900, ge=1)

    # HTTP
    max_body_bytes: int = Field(default=1024 * 1024, ge=1024)
    cors_origins: list[str] = Field(default_factory=list)

    # Documents / ingestion
    max_pdf_bytes: int = Field(default=25 * 1024 * 1024, ge=1024)
    max_pdf_pages: int = Field(default=200, ge=1)
    max_text_bytes: int = Field(default=10 * 1024 * 1024, ge=1024)  # txt, md, csv
    max_table_rows: int = Field(default=50_000, ge=1)
    max_csv_cols: int = Field(default=200, ge=1)
    docx_max_entries: int = Field(default=3000, ge=10)
    docx_max_uncompressed: int = Field(default=200 * 1024 * 1024, ge=1024)
    docx_max_ratio: int = Field(default=100, ge=2)
    upload_validation_timeout_s: float = Field(default=20.0, gt=0)
    rl_upload_limit: int = Field(default=30, ge=1)
    rl_upload_window_s: int = Field(default=3600, ge=1)
    pdf_pipeline: Literal["docling", "light"] = "docling"
    sandbox_mode: Literal["pool", "oneshot"] = "pool"  # warm children vs one process per call
    inspect_pool_size: int = Field(default=2, ge=1, le=16)
    parse_pool_size: int = Field(default=1, ge=1, le=8)  # per worker process; each child is heavy
    sandbox_recycle_jobs: int = Field(default=25, ge=1)
    inspect_timeout_s: float = Field(default=20.0, gt=0)
    inspect_memory_mb: int = Field(default=1024, ge=64)
    parse_timeout_base_s: float = Field(default=60.0, gt=0)
    parse_timeout_per_page_s: float = Field(default=4.0, gt=0)
    parse_timeout_max_s: float = Field(default=800.0, gt=0)
    parse_memory_mb: int = Field(default=6144, ge=256)
    docling_tables: bool = True
    docling_max_tokens: int = Field(default=320, ge=64, le=512)
    ocr_enabled: bool = True
    ocr_max_pages: int = Field(default=40, ge=1)
    ocr_dpi: int = Field(default=200, ge=72, le=400)
    min_text_chars_per_page: int = Field(default=25, ge=1)
    chunk_target_tokens: int = Field(default=256, ge=32)
    chunk_max_tokens: int = Field(default=384, ge=64)
    chunk_min_tokens: int = Field(default=48, ge=1)
    chunk_overlap_tokens: int = Field(default=40, ge=0)
    min_chunk_chars: int = Field(default=30, ge=1)
    model_cache_dir: Path = REPO_ROOT / "data" / "models"
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_dim: int = Field(default=384, ge=8)
    sparse_model: str = "Qdrant/bm25"
    embedding_batch_size: int = Field(default=32, ge=1, le=512)
    qdrant_chunks_collection: str = "chunks"

    # Retrieval (docs/spec/04-retrieval.md). Depths are per ranker before fusion.
    retrieval_dense_limit: int = Field(default=40, ge=1, le=500)
    retrieval_sparse_limit: int = Field(default=40, ge=1, le=500)
    rrf_k: int = Field(default=60, ge=1, le=1000)
    retrieval_top_k: int = Field(default=8, ge=1, le=100)
    retrieval_max_top_k: int = Field(default=30, ge=1, le=100)
    retrieval_timeout_s: float = Field(default=8.0, gt=0)
    retrieval_max_query_chars: int = Field(default=1000, ge=10, le=10000)
    retrieval_max_scope_documents: int = Field(default=50, ge=1, le=1000)
    # Dense query prefix (e.g. BGE's retrieval instruction); empty = none. Chosen by evaluation.
    dense_query_prefix: str = ""
    rerank_enabled: bool = True
    reranker_model: str = "BAAI/bge-reranker-v2-m3-int8"
    rerank_candidates: int = Field(default=5, ge=1, le=200)
    rerank_timeout_s: float = Field(default=10.0, gt=0)
    rerank_max_chars: int = Field(default=2000, ge=100)
    inference_threads: int = Field(default=2, ge=1, le=32)
    warm_models_on_startup: bool = False
    rl_search_limit: int = Field(default=120, ge=1)
    rl_search_window_s: int = Field(default=60, ge=1)
    outbox_batch_size: int = Field(default=50, ge=1, le=500)
    outbox_dispatch_interval_s: float = Field(default=5.0, ge=1.0)
    reconcile_interval_s: float = Field(default=60.0, ge=5.0)
    reconcile_queued_ttl_s: int = Field(default=120, ge=1)
    reconcile_processing_ttl_s: int = Field(default=1020, ge=1)
    reconcile_purge_ttl_s: int = Field(default=300, ge=1)
    ingestion_max_attempts: int = Field(default=5, ge=1)
    orphan_grace_s: int = Field(default=3600, ge=0)
    orphan_sweep_interval_s: float = Field(default=3600.0, ge=1.0)
    ingestion_retry_base_s: float = Field(default=5.0, gt=0)

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
        if not (
            self.chunk_min_tokens < self.chunk_target_tokens <= self.chunk_max_tokens
            and self.chunk_overlap_tokens < self.chunk_target_tokens
        ):
            raise ValueError(
                "chunk token settings must satisfy min < target <= max and overlap < target"
            )
        retired = self.jwt_retired_keys
        if self.jwt_key_id in retired:
            raise ValueError("jwt_retired_keys must not contain the active jwt_key_id")
        if any(len(v.get_secret_value()) < 32 for v in retired.values()):
            raise ValueError("every retired jwt key must be at least 32 characters")
        try:
            Fernet(self.mfa_encryption_key.get_secret_value().encode())
        except (ValueError, TypeError) as exc:
            raise ValueError("mfa_encryption_key must be a valid Fernet key") from exc
        if self.storage_backend == "s3" and not self.s3_bucket:
            raise ValueError("storage_backend=s3 requires s3_bucket")
        if self.smtp_use_ssl and self.smtp_starttls:
            raise ValueError("smtp_use_ssl and smtp_starttls are mutually exclusive")
        if self.app_env == "prod":
            if not self.require_verified_email:
                raise ValueError("prod requires require_verified_email=true")
            if self.email_backend != "smtp" or not self.smtp_host:
                raise ValueError("prod requires email_backend=smtp and smtp_host")
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
