"""Dense + sparse embedding behind a small protocol. Production uses FastEmbed (ONNX, local)."""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from typing import Any, NamedTuple, Protocol

from app.core.config import Settings


class SparseVec(NamedTuple):
    indices: list[int]
    values: list[float]


class Embedder(Protocol):
    model_name: str
    sparse_model_name: str
    dim: int

    def embed_dense(self, texts: Sequence[str]) -> list[list[float]]: ...

    def embed_sparse(self, texts: Sequence[str]) -> list[SparseVec]: ...


class FastEmbedEmbedder:
    """BAAI/bge-small-en-v1.5 dense vectors + Qdrant/bm25 sparse vectors, both run locally."""

    def __init__(self, settings: Settings) -> None:
        self.model_name = settings.embedding_model
        self.sparse_model_name = settings.sparse_model
        self.dim = settings.embedding_dim
        self._cache_dir = str(settings.model_cache_dir)
        self._dense: Any = None
        self._sparse: Any = None
        self._lock = threading.Lock()

    def _dense_model(self) -> Any:
        with self._lock:
            if self._dense is None:
                from fastembed import TextEmbedding

                self._dense = TextEmbedding(self.model_name, cache_dir=self._cache_dir)
            return self._dense

    def _sparse_model(self) -> Any:
        with self._lock:
            if self._sparse is None:
                from fastembed import SparseTextEmbedding

                self._sparse = SparseTextEmbedding(
                    self.sparse_model_name, cache_dir=self._cache_dir
                )
            return self._sparse

    def embed_dense(self, texts: Sequence[str]) -> list[list[float]]:
        return [[float(x) for x in vec] for vec in self._dense_model().embed(list(texts))]

    def embed_sparse(self, texts: Sequence[str]) -> list[SparseVec]:
        return [
            SparseVec([int(i) for i in e.indices], [float(v) for v in e.values])
            for e in self._sparse_model().embed(list(texts))
        ]


EmbedderFactory = Callable[[Settings], Embedder]

_factory: EmbedderFactory = FastEmbedEmbedder
_instance: Embedder | None = None
_instance_lock = threading.Lock()


def set_embedder_factory(factory: EmbedderFactory | None) -> None:
    """Swap the embedder implementation (tests install a deterministic adapter)."""
    global _factory, _instance
    with _instance_lock:
        _factory = factory or FastEmbedEmbedder
        _instance = None


def get_embedder(settings: Settings) -> Embedder:
    global _instance
    with _instance_lock:
        if _instance is None:
            _instance = _factory(settings)
        return _instance
