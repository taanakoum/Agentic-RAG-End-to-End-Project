"""Deletion across PostgreSQL, Qdrant and object storage (tombstone -> purge -> hard delete)."""

from __future__ import annotations

import uuid

from sqlalchemy import select

from app.chat.purge import delete_checkpoint_threads
from app.core.logging import get_logger
from app.infra.clients import create_sync_redis
from app.ingestion.indexing import delete_document_points, delete_user_points
from app.ingestion.runtime import WorkerRuntime
from app.ingestion.store import IngestionStore
from app.llm.cache import purge_user_cache_sync
from app.memory.index import delete_user_vectors
from app.models.chat import Conversation
from app.retrieval.cache import purge_user_retrieval_cache_sync

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


def _purge_llm_cache(rt: WorkerRuntime, user_id: uuid.UUID) -> None:
    """Cached answers contain the user's document text: remove them with the account."""
    redis = create_sync_redis(rt.settings)
    try:
        purge_user_cache_sync(redis, user_id)
        purge_user_retrieval_cache_sync(redis, user_id)
    finally:
        redis.close()


def purge_user(rt: WorkerRuntime, user_id: uuid.UUID) -> str:
    """Final step of account deletion: vectors, stored files, then the user row (cascades).

    Idempotent. The user row goes LAST so a failure is retried (task) or re-published (reconciler).
    Documents were already tombstoned when deletion was requested.
    """
    from app.models.user import User

    with rt.session_factory() as session:
        user = session.get(User, user_id)
        if user is None:
            return "already_purged"
        if user.deleted_at is None:
            return "not_deleted"
        if IngestionStore(session).has_processing_job_for_user(user_id):
            raise PurgeBusyError(str(user_id))
        delete_user_points(rt.qdrant, rt.settings, user_id=user_id)
        delete_user_vectors(rt.qdrant, rt.settings, user_id=user_id)
        _purge_llm_cache(rt, user_id)
        rt.storage.delete_prefix(str(user_id))
        threads = list(
            session.execute(
                select(Conversation.thread_id).where(Conversation.user_id == user_id)
            ).scalars()
        )
        delete_checkpoint_threads(session, threads)
        session.delete(user)  # sessions, tokens, MFA, documents and jobs cascade
        session.commit()
    log.info("user_purged", user_id=str(user_id))
    return "purged"
