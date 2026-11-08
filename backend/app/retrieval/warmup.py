"""Load the embedding and reranker models before the first user query needs them.

Model files are downloaded on first use and loaded lazily; a cold first search can take many
seconds and would hit the retrieval/rerank timeouts. Warm-up runs in the background on the
inference thread pool; it never blocks startup and a failure only produces a log line (searches
then degrade or fail with their normal, stable errors).
"""

from __future__ import annotations

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor

from app.core.config import Settings
from app.core.logging import get_logger
from app.ingestion.embedding import get_embedder
from app.retrieval.rerank import get_reranker

log = get_logger(__name__)


def _warm(settings: Settings) -> None:
    embedder = get_embedder(settings)
    embedder.embed_query_dense("warm up")
    embedder.embed_query_sparse("warm up")
    if settings.rerank_enabled:
        get_reranker(settings).score("warm up", ["warm up"])


async def warm_models(settings: Settings, executor: ThreadPoolExecutor) -> bool:
    """Returns True when every model loaded."""
    started = time.perf_counter()
    try:
        await asyncio.get_running_loop().run_in_executor(executor, _warm, settings)
    except Exception as exc:  # warm-up is best effort by design
        log.warning("model_warmup_failed", error=type(exc).__name__)
        return False
    log.info("model_warmup_done", seconds=round(time.perf_counter() - started, 1))
    return True
