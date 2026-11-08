"""Alembic environment. Uses the sync psycopg engine built from application settings."""

from __future__ import annotations

import sys
from pathlib import Path

# alembic's prepend_sys_path splits on whitespace, which breaks paths containing spaces.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from alembic import context

from app.core.config import get_settings
from app.database.base import Base
from app.database.session import create_sync_db_engine

# Import model modules here so that autogenerate sees them (added as models are created).

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = get_settings().database_url(driver="psycopg").render_as_string(hide_password=False)
    context.configure(
        url=url, target_metadata=target_metadata, literal_binds=True, compare_type=True
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_sync_db_engine(get_settings())
    try:
        with engine.connect() as connection:
            context.configure(
                connection=connection, target_metadata=target_metadata, compare_type=True
            )
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
