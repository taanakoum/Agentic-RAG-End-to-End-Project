"""Test environment. Must run before any `app` module reads settings."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.chat.checkpointer import use_selector_loop

use_selector_loop()  # before any event loop exists (psycopg async needs it on Windows)

_TMP_ROOT = Path(tempfile.mkdtemp(prefix="rag-tests-"))

# Environment variables take priority over .env, so tests can never touch the dev database.
os.environ["APP_ENV"] = "test"
os.environ["POSTGRES_DB"] = "rag_test"
os.environ["REDIS_URL"] = "redis://127.0.0.1:6379/15"
os.environ["QDRANT_COLLECTION_PREFIX"] = "test_"
os.environ["STORAGE_LOCAL_ROOT"] = str(_TMP_ROOT / "objects")
os.environ["LOG_LEVEL"] = "WARNING"
os.environ["JWT_SECRET"] = "test-only-jwt-secret-not-used-anywhere-else-0123456789"
# Cheap Argon2 parameters keep the suite fast; the prod validator still enforces OWASP minimums.
os.environ["MFA_ENCRYPTION_KEY"] = "dGVzdC1vbmx5LWZlcm5ldC1rZXktMzItYnl0ZXMhISE="
os.environ["EMAIL_FILE_DIR"] = str(_TMP_ROOT / "outbox")
# the deterministic HashEmbedder has its own score scale; the real default (0.6) is calibrated
# for the real model in evaluation/memory/calibrate.py and tested in the real_model test
os.environ["MEMORY_MIN_SCORE"] = "0.5"
# The test suite must NEVER reach a hosted model, even if the developer put a key in .env.
# Environment variables beat .env, so blank them; real_llm tests read REAL_LLM_* explicitly.
for _name in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL", "LLM_FALLBACK_MODELS"):
    os.environ[_name] = ""
os.environ["PDF_PIPELINE"] = "light"  # fast; Docling is exercised by dedicated tests
os.environ["INGESTION_MAX_ATTEMPTS"] = "3"
os.environ["INGESTION_RETRY_BASE_S"] = "0.05"
os.environ["RECONCILE_QUEUED_TTL_S"] = "1"
os.environ["RECONCILE_PROCESSING_TTL_S"] = "1"
os.environ["RECONCILE_PURGE_TTL_S"] = "1"
os.environ["MODEL_CACHE_DIR"] = str(Path(__file__).resolve().parents[2] / "data" / "models")
os.environ["ARGON2_MEMORY_KIB"] = "8192"
os.environ["ARGON2_TIME_COST"] = "1"
os.environ["ARGON2_PARALLELISM"] = "1"
# Generous per-IP limits so ordinary tests are not throttled; limit tests build their own settings.
for _name in ("LOGIN_IP", "REGISTER_IP", "REFRESH_IP", "FORGOT_IP", "RESET_IP"):
    os.environ[f"RL_{_name}_LIMIT"] = "100000"


# Developer-specific .env values must not change test behaviour (limits are tested explicitly).
for _name, _value in (
    ("LLM_DAILY_REQUEST_BUDGET", "0"),
    ("LLM_REQUESTS_PER_MINUTE", "0"),
    ("RL_CHAT_DAILY_LIMIT", "0"),
    ("LLM_MAX_CONCURRENCY", "4"),
    ("LLM_MAX_CONCURRENCY_PER_USER", "2"),
    ("LLM_MAX_TOKENS", "1024"),
    ("LLM_JSON_MODE", "json_object"),
    ("OTEL_ENABLED", "false"),
    ("OTEL_EXPORTER_OTLP_ENDPOINT", ""),
):
    os.environ[_name] = _value

from app.core.config import get_settings  # noqa: E402
from tests import telemetry_helpers  # noqa: E402

telemetry_helpers.install(get_settings())  # spans/metrics are captured in memory for assertions


@pytest.fixture(autouse=True)
def _fresh_spans() -> Iterator[None]:
    telemetry_helpers.EXPORTER.clear()
    yield


@pytest.fixture(scope="session")
def tmp_root() -> Iterator[Path]:
    yield _TMP_ROOT


# ---------------------------------------------------------------- test categories
# One command per category (scripts/test.ps1). Categories are assigned by file here, in one place,
# so that a new test file is classified by editing a single table; a file may be in several.
_CATEGORY_FILES: dict[str, frozenset[str]] = {
    "e2e": frozenset(
        {
            "test_chat_e2e.py",
            "test_retrieval_e2e.py",
            "test_documents_pipeline.py",
            "test_memory_pipeline.py",
            "test_chat_lifecycle.py",
            "test_frontend_api.py",
            "test_frontend_ui.py",
            "test_frontend_flows.py",
            "test_observability.py",
        }
    ),
    "security": frozenset(
        {
            "test_auth_api.py",
            "test_auth_ownership.py",
            "test_mfa_api.py",
            "test_account_api.py",
            "test_account_lifecycle.py",
            "test_rate_limiter.py",
            "test_security.py",
            "test_sandbox.py",
            "test_warm_sandbox.py",
            "test_key_rotation_email_crypto.py",
            "test_telemetry.py",
            "test_feedback_api.py",
            "test_outages.py",
        }
    ),
    "concurrency": frozenset(
        {
            "test_concurrency.py",
            "test_chat_turns.py",
            "test_llm_quota.py",
            "test_rate_limiter.py",
            "test_checkpointer.py",
            "test_orphans.py",
        }
    ),
    "failure": frozenset(
        {
            "test_workers_down.py",
            "test_orphans.py",
            "test_celery_and_health.py",
            "test_llm_cache.py",
            "test_llm_quota.py",
            "test_storage_contract.py",
            "test_outages.py",
            "test_agent_eval.py",
        }
    ),
}


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        path = Path(str(item.fspath))
        if path.parent.name == "unit":
            item.add_marker(pytest.mark.unit)
        for category, files in _CATEGORY_FILES.items():
            if path.name in files:
                item.add_marker(getattr(pytest.mark, category))
