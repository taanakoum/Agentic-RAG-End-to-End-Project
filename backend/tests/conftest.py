"""Test environment. Must run before any `app` module reads settings."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

_TMP_ROOT = Path(tempfile.mkdtemp(prefix="rag-tests-"))

# Environment variables take priority over .env, so tests can never touch the dev database.
os.environ["APP_ENV"] = "test"
os.environ["POSTGRES_DB"] = "rag_test"
os.environ["REDIS_URL"] = "redis://127.0.0.1:6379/15"
os.environ["QDRANT_COLLECTION_PREFIX"] = "test_"
os.environ["STORAGE_LOCAL_ROOT"] = str(_TMP_ROOT / "objects")
os.environ["LOG_LEVEL"] = "WARNING"


@pytest.fixture(scope="session")
def tmp_root() -> Iterator[Path]:
    yield _TMP_ROOT
