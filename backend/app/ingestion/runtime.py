"""Per-process resources for Celery workers (sync DB, storage, Qdrant, embedder, OCR)."""

from __future__ import annotations

import threading
from dataclasses import dataclass

from qdrant_client import QdrantClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings, get_settings
from app.database.session import create_sync_db_engine, sync_session_factory
from app.infra.clients import create_sync_qdrant
from app.ingestion.embedding import Embedder, get_embedder
from app.ingestion.extraction import OcrEngine
from app.ingestion.ocr import RapidOcrEngine
from app.storage.base import ObjectStorage
from app.storage.factory import create_storage


@dataclass(slots=True)
class WorkerRuntime:
    settings: Settings
    engine: Engine
    session_factory: sessionmaker[Session]
    storage: ObjectStorage
    qdrant: QdrantClient
    embedder: Embedder
    ocr: OcrEngine | None

    @classmethod
    def create(cls, settings: Settings) -> WorkerRuntime:
        engine = create_sync_db_engine(settings)
        return cls(
            settings=settings,
            engine=engine,
            session_factory=sync_session_factory(engine),
            storage=create_storage(settings),
            qdrant=create_sync_qdrant(settings),
            embedder=get_embedder(settings),
            ocr=RapidOcrEngine() if settings.ocr_enabled else None,
        )

    def close(self) -> None:
        self.engine.dispose()
        self.qdrant.close()


_runtime: WorkerRuntime | None = None
_lock = threading.Lock()


def get_runtime() -> WorkerRuntime:
    global _runtime
    with _lock:
        if _runtime is None:
            _runtime = WorkerRuntime.create(get_settings())
        return _runtime


def set_runtime(runtime: WorkerRuntime | None) -> None:
    """Install (tests) or reset the process-wide runtime."""
    global _runtime
    with _lock:
        _runtime = runtime
