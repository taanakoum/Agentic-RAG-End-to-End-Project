"""Cross-encoder reranking behind a small protocol (FastEmbed ONNX, runs locally on CPU)."""

from __future__ import annotations

import importlib
import threading
from collections.abc import Callable, Sequence
from typing import Any, Protocol

from app.core.config import Settings


class Reranker(Protocol):
    model_name: str

    def score(self, query: str, passages: Sequence[str]) -> list[float]:
        """One relevance score per passage (higher = more relevant), in input order."""
        ...


class FastEmbedReranker:
    def __init__(self, settings: Settings) -> None:
        self.model_name = settings.reranker_model
        self._cache_dir = str(settings.model_cache_dir)
        self._model: Any = None
        self._lock = threading.Lock()

    def _load(self) -> Any:
        with self._lock:
            if self._model is None:
                module: Any = importlib.import_module("fastembed.rerank.cross_encoder")
                self._model = module.TextCrossEncoder(self.model_name, cache_dir=self._cache_dir)
            return self._model

    def score(self, query: str, passages: Sequence[str]) -> list[float]:
        if not passages:
            return []
        return [float(x) for x in self._load().rerank(query, list(passages))]


RerankerFactory = Callable[[Settings], Reranker]

_factory: RerankerFactory = FastEmbedReranker
_instance: Reranker | None = None
_lock = threading.Lock()


def set_reranker_factory(factory: RerankerFactory | None) -> None:
    """Swap the implementation (tests install a deterministic adapter)."""
    global _factory, _instance
    with _lock:
        _factory = factory or FastEmbedReranker
        _instance = None


def get_reranker(settings: Settings) -> Reranker:
    global _instance
    with _lock:
        if _instance is None:
            _instance = _factory(settings)
        return _instance
