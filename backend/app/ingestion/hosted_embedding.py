"""Dense embeddings from an OpenAI-compatible `/embeddings` endpoint (free-tier variant, ADR-042).

Local ONNX models do not fit a 512 MB free instance next to the API, so the dense vectors come from
the hosted model (for example Gemini's `gemini-embedding-001` through its OpenAI-compatible API).
BM25 sparse vectors are still computed locally: they need no model weights. Document text is sent
to the embedding provider, like questions are sent to the language model (KNOWN_ISSUES A5).
"""

from __future__ import annotations

import math
import time
from collections.abc import Sequence
from typing import Any

import httpx

from app.core.config import Settings
from app.core.logging import get_logger
from app.ingestion.embedding import FastEmbedEmbedder, SparseVec

log = get_logger(__name__)

_RETRY_STATUS = {429, 500, 502, 503, 504}


class EmbeddingServiceError(RuntimeError):
    """The hosted embedding service failed or returned something unusable."""


def _fit(vector: Sequence[float], dim: int) -> list[float]:
    """Truncate to `dim` and re-normalise (Matryoshka models keep quality when truncated)."""
    if len(vector) < dim:
        raise EmbeddingServiceError(f"embedding has {len(vector)} dimensions, expected {dim}")
    cut = [float(x) for x in vector[:dim]]
    norm = math.sqrt(sum(x * x for x in cut))
    if norm == 0.0:
        raise EmbeddingServiceError("embedding is all zeros")
    return [x / norm for x in cut]


class HostedEmbedder:
    def __init__(self, settings: Settings, *, client: httpx.Client | None = None) -> None:
        self.model_name = settings.embedding_model
        self.sparse_model_name = settings.sparse_model
        self.dim = settings.embedding_dim
        self._batch = settings.embedding_api_batch_size
        self._attempts = settings.embedding_api_max_attempts
        base = (settings.embedding_api_base_url or settings.llm_base_url).rstrip("/") + "/"
        key = (
            settings.embedding_api_key.get_secret_value() or settings.llm_api_key.get_secret_value()
        )
        if not base.strip("/") or not key:
            raise EmbeddingServiceError(
                "hosted embeddings need EMBEDDING_API_BASE_URL/LLM_BASE_URL and a key"
            )
        self._auth = {
            "Authorization": f"Bearer {key}"
        }  # sent per request, also on injected clients
        self._http = client or httpx.Client(base_url=base, timeout=settings.embedding_api_timeout_s)
        self._sparse = FastEmbedEmbedder(settings)  # used for BM25 only; no dense model is loaded

    def _post(self, batch: list[str]) -> list[list[float]]:
        body: dict[str, Any] = {"model": self.model_name, "input": batch, "dimensions": self.dim}
        for attempt in range(1, self._attempts + 1):
            try:
                response = self._http.post("embeddings", json=body, headers=self._auth)
            except httpx.TransportError as exc:
                if attempt == self._attempts:
                    raise EmbeddingServiceError(
                        f"embedding request failed: {type(exc).__name__}"
                    ) from exc
                time.sleep(min(2.0**attempt, 30.0))
                continue
            if response.status_code in _RETRY_STATUS and attempt < self._attempts:
                retry_after = response.headers.get("retry-after", "")
                delay = (
                    float(retry_after)
                    if retry_after.replace(".", "", 1).isdigit()
                    else 2.0**attempt
                )
                log.warning("embedding_retry", status=response.status_code, attempt=attempt)
                time.sleep(min(delay, 60.0))
                continue
            if response.status_code != 200:
                raise EmbeddingServiceError(f"embedding service answered {response.status_code}")
            data = sorted(response.json().get("data", []), key=lambda d: d.get("index", 0))
            if len(data) != len(batch):
                raise EmbeddingServiceError(
                    "embedding service returned the wrong number of vectors"
                )
            return [_fit(item["embedding"], self.dim) for item in data]
        raise EmbeddingServiceError("embedding service kept failing")

    def embed_dense(self, texts: Sequence[str]) -> list[list[float]]:
        items = list(texts)
        out: list[list[float]] = []
        for start in range(0, len(items), self._batch):
            out.extend(self._post(items[start : start + self._batch]))
        return out

    def embed_query_dense(self, text: str) -> list[float]:
        return self._post([text])[0]

    def embed_sparse(self, texts: Sequence[str]) -> list[SparseVec]:
        return self._sparse.embed_sparse(texts)

    def embed_query_sparse(self, text: str) -> SparseVec:
        return self._sparse.embed_query_sparse(text)
