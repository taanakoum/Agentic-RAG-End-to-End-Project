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


@pytest.fixture(scope="session")
def tmp_root() -> Iterator[Path]:
    yield _TMP_ROOT
