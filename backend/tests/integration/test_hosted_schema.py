"""Exercise a fresh private schema through real migrations and every database client."""

from __future__ import annotations

import uuid
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateSchema, DropSchema

from app.chat.checkpointer import CheckpointerProvider, conninfo
from app.chat.purge import delete_checkpoint_threads, sweep_orphan_checkpoints
from app.core import config
from app.core.config import get_settings
from app.database.session import create_async_db_engine, create_sync_db_engine
from app.infra.container import Infrastructure
from tests.integration.test_checkpointer import build_graph
from tests.integration.test_checkpointer import config as graph_config

pytestmark = pytest.mark.integration
ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"


async def test_private_schema_migrations_api_workers_and_checkpoints(
    monkeypatch: pytest.MonkeyPatch,
    infra: Infrastructure,
) -> None:
    base = get_settings()
    assert base.app_env == "test" and base.postgres_db.endswith("_test")
    schema = "deploy_" + uuid.uuid4().hex[:16]
    settings = base.model_copy(update={"postgres_schema": schema})
    admin = create_sync_db_engine(base)
    api = create_async_db_engine(settings)
    worker = create_sync_db_engine(settings)
    provider = CheckpointerProvider(settings)
    try:
        # Public checkpoint tables must not make an uninitialised private schema appear ready.
        await infra.checkpointer.get()
        with admin.begin() as db:
            db.execute(CreateSchema(schema))
        # Alembic's env imports this function; other fixtures retain their own base settings.
        monkeypatch.setattr(config, "get_settings", lambda: settings)
        command.upgrade(Config(str(ALEMBIC_INI)), "head")
        with worker.connect() as db:
            assert db.execute(text("SELECT current_schema()")).scalar() == schema
            assert db.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0010"
            assert db.execute(text("SELECT count(*) FROM users")).scalar() == 0
        async with api.connect() as db:
            assert (await db.execute(text("SELECT current_schema()"))).scalar() == schema
            assert (await db.execute(text("SELECT count(*) FROM users"))).scalar() == 0
        with Session(worker) as db:
            assert delete_checkpoint_threads(db, [uuid.uuid4()]) == 0
        graph = build_graph(await provider.get())
        thread = uuid.uuid4()
        await graph.ainvoke({"n": 0, "log": ["hosted"]}, graph_config(thread))
        assert (await graph.aget_state(graph_config(thread))).values["n"] == 1
        with Session(worker) as db:
            assert delete_checkpoint_threads(db, [thread]) > 0
            db.commit()
        assert (await graph.aget_state(graph_config(thread))).values == {}
        await graph.ainvoke({"n": 0, "log": ["orphan"]}, graph_config(uuid.uuid4()))
        with Session(worker) as db:
            assert sweep_orphan_checkpoints(db) > 0
        async with await psycopg.AsyncConnection.connect(conninfo(settings)) as db:
            result = await db.execute(
                "SELECT table_schema FROM information_schema.tables "
                "WHERE table_schema = %s AND table_name = 'checkpoints'",
                (schema,),
            )
            assert await result.fetchone() == (schema,)
    finally:
        await provider.close()
        await api.dispose()
        worker.dispose()
        with admin.begin() as db:
            db.execute(DropSchema(schema, cascade=True, if_exists=True))
        admin.dispose()
