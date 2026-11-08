"""Subprocess used by the crash-recovery test: starts ingesting, hangs in the embedder, gets killed.

Run as: python backend/tests/crash_worker.py <job_id>   (cwd=backend, PYTHONPATH=backend)
Prints EMBEDDING when it reaches the embedding stage so the parent can kill it mid-job.
"""

from __future__ import annotations

import sys
import time
import uuid
from collections.abc import Sequence

from app.core.config import get_settings
from app.ingestion.embedding import set_embedder_factory
from app.ingestion.pipeline import run_ingestion
from app.ingestion.runtime import WorkerRuntime, set_runtime
from tests.fakes import HashEmbedder


class HangingEmbedder(HashEmbedder):
    def embed_dense(self, texts: Sequence[str]) -> list[list[float]]:
        print("EMBEDDING", flush=True)
        time.sleep(600)
        return super().embed_dense(texts)


def main() -> None:
    set_embedder_factory(HangingEmbedder)
    runtime = WorkerRuntime.create(get_settings())
    set_runtime(runtime)
    run_ingestion(runtime, uuid.UUID(sys.argv[1]))


if __name__ == "__main__":
    main()
