"""Helpers for retrieval tests: seed documents and chunks directly (no worker needed)."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from qdrant_client import QdrantClient
from sqlalchemy import text

from app.core.config import get_settings
from app.infra.container import Infrastructure
from app.ingestion.chunking import Chunk, estimate_tokens
from app.ingestion.embedding import SparseVec
from app.ingestion.indexing import upsert_chunks
from app.models.document import Document
from tests.fakes import HashEmbedder


def make_chunks(texts: Sequence[str], *, section: str = "", page: int = 1) -> list[Chunk]:
    return [
        Chunk(
            index=i,
            page_number=page + i,
            page_end=page + i,
            heading=section or None,
            section_path=section,
            text=t,
            tokens=estimate_tokens(t),
            embed_text=t,
        )
        for i, t in enumerate(texts)
    ]


async def seed_document(
    infra: Infrastructure,
    user_id: uuid.UUID,
    filename: str,
    texts: Sequence[str],
    *,
    status: str = "completed",
    doc_version: int = 1,
    index_version: int | None = None,
    deleted: bool = False,
    section: str = "",
    dense: Sequence[Sequence[float]] | None = None,
    sparse: Sequence[SparseVec] | None = None,
) -> uuid.UUID:
    """Insert a document row and index its chunks (HashEmbedder unless vectors are given)."""
    settings = get_settings()
    document_id = uuid.uuid4()
    async with infra.session_factory() as session:
        session.add(
            Document(
                id=document_id,
                user_id=user_id,
                filename=filename,
                content_hash=hashlib.sha256(str(document_id).encode()).hexdigest(),
                size_bytes=1,
                mime_type="application/pdf",
                storage_key=f"{user_id}/{document_id}/original.pdf",
                status=status,
                version=doc_version,
                page_count=len(texts),
                chunk_count=len(texts),
                deleted_at=datetime.now(UTC) if deleted else None,
            )
        )
        await session.commit()
    chunks = make_chunks(texts, section=section)
    embedder = HashEmbedder(settings)
    client = QdrantClient(url=settings.qdrant_url, timeout=30)
    try:
        upsert_chunks(
            client,
            settings,
            user_id=user_id,
            document_id=document_id,
            version=index_version if index_version is not None else doc_version,
            filename=filename,
            chunks=chunks,
            dense=list(dense)
            if dense is not None
            else embedder.embed_dense([c.text for c in chunks]),
            sparse=list(sparse)
            if sparse is not None
            else embedder.embed_sparse([c.text for c in chunks]),
            embedding_model=embedder.model_name,
        )
    finally:
        client.close()
    return document_id


async def set_document(infra: Infrastructure, document_id: uuid.UUID, **columns: object) -> None:
    sets = ", ".join(f"{name} = :{name}" for name in columns)
    async with infra.session_factory() as session:
        await session.execute(
            text(f"UPDATE documents SET {sets} WHERE id = :id"),  # noqa: S608
            {"id": document_id, **columns},
        )
        await session.commit()
