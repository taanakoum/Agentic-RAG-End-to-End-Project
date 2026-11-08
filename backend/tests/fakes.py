"""TEST-ONLY adapters. Never imported by application code.

`HashEmbedder` is a deterministic stand-in for the real embedding model so ingestion tests do
not download models or depend on them; it still produces real vectors of the configured size,
so Qdrant behaviour (dimensions, sparse vectors, filtering) is exercised for real.
"""

from __future__ import annotations

import hashlib
import math
import re
import threading
from collections import Counter
from collections.abc import Callable, Sequence

from app.core.config import Settings
from app.ingestion.embedding import SparseVec

_TOKEN = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


class HashEmbedder:
    model_name = "test-hash-embedder"
    sparse_model_name = "test-hash-sparse"

    def __init__(self, settings: Settings) -> None:
        self.dim = settings.embedding_dim
        self.calls = 0
        self.fail_next: int = 0  # raise on the next N embed_dense calls (transient failure)
        self.on_dense: Callable[[], None] | None = None
        self._lock = threading.Lock()

    def embed_dense(self, texts: Sequence[str]) -> list[list[float]]:
        with self._lock:
            self.calls += 1
            if self.fail_next > 0:
                self.fail_next -= 1
                raise ConnectionError("simulated embedding provider outage")
        if self.on_dense is not None:
            self.on_dense()
        vectors: list[list[float]] = []
        for text in texts:
            vec = [0.0] * self.dim
            for token in _tokens(text):
                bucket = (
                    int.from_bytes(hashlib.sha256(token.encode()).digest()[:4], "big") % self.dim
                )
                vec[bucket] += 1.0
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            vectors.append([v / norm for v in vec])
        return vectors

    def embed_sparse(self, texts: Sequence[str]) -> list[SparseVec]:
        out: list[SparseVec] = []
        for text in texts:
            counts = Counter(
                int.from_bytes(hashlib.sha256(t.encode()).digest()[:4], "big")
                for t in _tokens(text)
            )
            items = sorted(counts.items())
            out.append(SparseVec([i for i, _ in items], [float(c) for _, c in items]))
        return out
