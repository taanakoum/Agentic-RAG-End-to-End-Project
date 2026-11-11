"""The ingestion pipeline for one job. Idempotent: safe to run again after a crash or redelivery."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Sequence
from pathlib import Path

from app.core.logging import get_logger
from app.core.telemetry import instruments, span, timed
from app.ingestion.chunking import Chunk
from app.ingestion.embedding import SparseVec
from app.ingestion.indexing import (
    delete_document_points,
    delete_stale_versions,
    upsert_chunks,
)
from app.ingestion.parsing import ParseError, parse_bytes
from app.ingestion.runtime import WorkerRuntime
from app.ingestion.states import DocumentStatus, JobStatus
from app.ingestion.store import IngestionStore, JobClaim, LostStateError

log = get_logger(__name__)

S = DocumentStatus


class PermanentIngestionError(Exception):
    """A failure retrying cannot fix (corrupt/encrypted/empty document, integrity mismatch)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def object_key(user_id: uuid.UUID, document_id: uuid.UUID, source_format: str = "pdf") -> str:
    return f"{user_id}/{document_id}/original.{source_format}"


def _embed(rt: WorkerRuntime, chunks: Sequence[Chunk]) -> tuple[list[list[float]], list[SparseVec]]:
    batch = rt.settings.embedding_batch_size
    dense: list[list[float]] = []
    sparse: list[SparseVec] = []
    for start in range(0, len(chunks), batch):
        texts = [c.embed_text or c.text for c in chunks[start : start + batch]]
        dense.extend(rt.embedder.embed_dense(texts))
        sparse.extend(rt.embedder.embed_sparse(texts))
    if len(dense) != len(chunks) or len(sparse) != len(chunks):
        raise RuntimeError("embedder returned a different number of vectors than chunks")
    return dense, sparse


def run_ingestion(rt: WorkerRuntime, job_id: uuid.UUID) -> str:
    """Process one ingestion job. Returns an outcome label.

    Raises for transient failures (the Celery task decides about retry); permanent failures are
    recorded on the document and returned as the outcome "failed".
    """
    with rt.session_factory() as session:
        store = IngestionStore(session)
        claim = store.claim_job(job_id)
        if isinstance(claim, str):
            log.info("ingestion_skipped", job_id=str(job_id), reason=claim)
            return claim
        try:
            return _process(rt, store, claim)
        except PermanentIngestionError as exc:
            session.rollback()
            store.mark_failed(
                claim.document_id,
                claim.job_id,
                code=exc.code,
                message=exc.message,
                recoverable=False,
            )
            log.warning("ingestion_failed_permanently", job_id=str(job_id), code=exc.code)
            return "failed"
        except LostStateError:
            session.rollback()
            # deleted or moved by someone else mid-run: stop quietly; purge/reconcile will tidy up
            store.finish_job(claim.job_id, JobStatus.SUPERSEDED, "document state changed")
            return "superseded"


def _process(rt: WorkerRuntime, store: IngestionStore, claim: JobClaim) -> str:
    settings = rt.settings
    doc = store.get_document(claim.document_id)
    if doc is None or doc.deleted_at is not None:
        store.finish_job(claim.job_id, JobStatus.SUPERSEDED, "document deleted")
        return "document_deleted"
    if doc.user_id != claim.user_id:  # workers verify ownership, never trust the message alone
        raise PermanentIngestionError(
            "OWNERSHIP_MISMATCH", "Job does not belong to the document owner."
        )
    if doc.status != S.QUEUED.value:
        # e.g. already completed by an earlier delivery; nothing to do
        store.finish_job(claim.job_id, JobStatus.SUPERSEDED, f"document is {doc.status}")
        return f"document_{doc.status}"

    user_id, document_id, version, filename = doc.user_id, doc.id, doc.version, doc.filename
    content_hash, key = doc.content_hash, doc.storage_key

    store.transition(document_id, S.QUEUED, S.PROCESSING)
    store.transition(document_id, S.PROCESSING, S.EXTRACTING)

    # --- parse + chunk in the sandbox (Docling or light pipeline; OCR only if a page needs it) ---
    with rt.storage.open(key) as handle:
        data = handle.read()
    if hashlib.sha256(data).hexdigest() != content_hash:
        raise PermanentIngestionError(
            "STORAGE_INTEGRITY", "The stored file does not match its recorded checksum."
        )
    try:
        with (
            span("ingestion.parse", **{"ingestion.format": doc.source_format}) as stage,
            timed() as took,
        ):
            parsed = parse_bytes(
                rt.parser,
                data,
                title=Path(filename).stem or "document",
                page_count=doc.page_count or 1,
                source_format=doc.source_format,
            )
            stage.set_attribute("ingestion.pages", parsed.page_count)
            stage.set_attribute("ingestion.chunks", len(parsed.chunks))
            stage.set_attribute("ingestion.parser", parsed.parser)
            stage.set_attribute("ingestion.ocr_pages", parsed.ocr_pages)
        instruments().ingestion_stage.record(took[0], {"stage": "parse", "parser": parsed.parser})
    except ParseError as exc:
        raise PermanentIngestionError(exc.code, exc.message) from exc
    if not parsed.chunks:
        raise PermanentIngestionError("DOCUMENT_NO_TEXT", _no_text_message(parsed.ocr_status))
    store.transition(
        document_id,
        S.EXTRACTING,
        S.CHUNKING,
        page_count=parsed.page_count,
        ocr_status=parsed.ocr_status,
        ocr_pages=parsed.ocr_pages,
        parser=parsed.parser,
    )
    chunks = parsed.chunks
    store.transition(document_id, S.CHUNKING, S.EMBEDDING)

    # --- embed ---------------------------------------------------------------------------------
    with span("ingestion.embed", **{"ingestion.chunks": len(chunks)}), timed() as took:
        dense, sparse = _embed(rt, chunks)
    instruments().ingestion_stage.record(took[0], {"stage": "embed"})
    store.transition(document_id, S.EMBEDDING, S.INDEXING)

    # --- index (deterministic point ids: re-running overwrites, never duplicates) -----------------
    with span("ingestion.index", **{"ingestion.chunks": len(chunks)}), timed() as took:
        upsert_chunks(
            rt.qdrant,
            settings,
            user_id=user_id,
            document_id=document_id,
            version=version,
            filename=filename,
            chunks=chunks,
            dense=dense,
            sparse=sparse,
            embedding_model=rt.embedder.model_name,
        )
    instruments().ingestion_stage.record(took[0], {"stage": "index"})
    delete_stale_versions(
        rt.qdrant, settings, user_id=user_id, document_id=document_id, keep=version
    )

    # The document may have been deleted while we were indexing: do not leave orphan vectors.
    latest = store.get_document(document_id)
    if latest is None or latest.deleted_at is not None:
        delete_document_points(rt.qdrant, settings, user_id=user_id, document_id=document_id)
        store.finish_job(claim.job_id, JobStatus.SUPERSEDED, "document deleted during indexing")
        return "document_deleted"

    store.complete(
        document_id, claim.job_id, chunk_count=len(chunks), embedding_model=rt.embedder.model_name
    )
    log.info("ingestion_completed", document_id=str(document_id), chunks=len(chunks))
    return "completed"


def _no_text_message(ocr_status: str) -> str:
    base = "No extractable text was found in this document."
    if ocr_status == "disabled":
        return base + " It looks scanned and OCR is disabled."
    if ocr_status == "failed":
        return base + " OCR was attempted but recognised no usable text."
    if ocr_status in {"used", "limited"}:
        return base + " OCR ran but recognised no usable text."
    return base
