"""Hosted DB transport must agree across API, workers, migrations and checkpoints."""

from __future__ import annotations

import ssl
from pathlib import Path
from unittest.mock import patch

import pytest
from psycopg.conninfo import conninfo_to_dict
from sqlalchemy.ext.asyncio import create_async_engine

from app.chat.checkpointer import conninfo
from app.database.session import create_async_db_engine, create_sync_db_engine
from tests.unit.test_config import make


def test_psycopg_and_checkpointer_preserve_tls_schema_and_escaped_password() -> None:
    s = make(
        postgres_password="p@ss:w/rd%#?",
        postgres_sslmode="verify-full",
        postgres_sslrootcert=Path("/etc/secrets/supabase-ca.crt"),
        postgres_schema="rag",
    )
    parsed = conninfo_to_dict(conninfo(s))
    assert parsed["password"] == "p@ss:w/rd%#?"
    assert parsed["sslmode"] == "verify-full"
    root_cert = parsed["sslrootcert"]
    assert isinstance(root_cert, str)
    assert Path(root_cert) == s.postgres_sslrootcert
    assert parsed["options"] == "-csearch_path=rag"
    assert parsed["connect_timeout"] == str(s.db_connect_timeout_s)
    engine = create_sync_db_engine(s)
    try:
        assert engine.url.query["sslmode"] == "verify-full"
        assert engine.url.query["options"] == "-csearch_path=rag"
    finally:
        engine.dispose()


async def test_asyncpg_enforces_ca_and_hostname_verification_without_libpq_url_options() -> None:
    s = make(postgres_sslmode="verify-full", postgres_schema="rag")
    # Observe the real engine factory's input without ever opening a network connection.
    with patch(
        "app.database.session.create_async_engine",
        wraps=create_async_engine,
    ) as factory:
        engine = create_async_db_engine(s)
        try:
            args = factory.call_args.kwargs["connect_args"]
            tls = args["ssl"]
            assert isinstance(tls, ssl.SSLContext)
            assert tls.check_hostname and tls.verify_mode == ssl.CERT_REQUIRED
            assert args["server_settings"] == {"search_path": "rag"}
            assert "sslmode" not in engine.url.query
        finally:
            await engine.dispose()


def test_missing_ca_fails_instead_of_falling_back_to_plaintext(tmp_path: Path) -> None:
    s = make(postgres_sslmode="verify-full", postgres_sslrootcert=tmp_path / "missing.crt")
    with pytest.raises(FileNotFoundError):
        create_async_db_engine(s)
