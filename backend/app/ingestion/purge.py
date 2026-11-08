"""Deletion across PostgreSQL, Qdrant and object storage (tombstone -> purge -> hard delete)."""

from __future__ import annotations

import uuid

from app.core.logging import get_logger
from app.ingestion.indexing import delete_document_points
from app.ingestion.runtime import WorkerRuntime
from app.ingestion.store import IngestionStore

log = get_logger(__name__)


class PurgeBusyError(Exception):
    """A job is still processing this document; try again later."""


def purge_document(rt: WorkerRuntime, document_id: uuid.UUID, user_id: uuid.UUID) -> str:
    """Remove vectors, stored files and finally the rows. Idempotent and safe to repeat.

    The PostgreSQL row is deleted LAST: until it is gone the tombstone stays visible to the
    reconciler, so a failure in Qdrant or storage is retried instead of forgotten.
    """
    with rt.session_factory() as session:
        store = IngestionStore(session)
        doc = store.get_document(document_id)
        if doc is None:
            return "already_purged"
        if doc.user_id != user_id:
            log.error("purge_ownership_mismatch", document_id=str(document_id))
            return "ownership_mismatch"
        if doc.deleted_at is None:
            return "not_deleted"
        if store.has_processing_job(document_id):
            raise PurgeBusyError(str(document_id))
        delete_document_points(rt.qdrant, rt.settings, user_id=user_id, document_id=document_id)
        rt.storage.delete_prefix(f"{user_id}/{document_id}")
        store.hard_delete_document(document_id)
    log.info("document_purged", document_id=str(document_id))
    return "purged"
