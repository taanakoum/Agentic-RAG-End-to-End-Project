"""Deletion of conversations and their LangGraph checkpoints (tombstone -> purge -> hard delete).

Checkpoints live in the tables created by `langgraph-checkpoint-postgres` (`checkpoints`,
`checkpoint_blobs`, `checkpoint_writes`, all keyed by `thread_id` text). Deleting them with SQL in
the SAME transaction as the conversation row makes the purge atomic. A contract test proves this
removes exactly what the library's own `aget_tuple` can see, so a library change is caught.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.models.chat import Conversation

log = get_logger(__name__)

CHECKPOINT_TABLES = ("checkpoints", "checkpoint_blobs", "checkpoint_writes")


def _tables_exist(session: Session) -> bool:
    return bool(
        session.execute(text("SELECT to_regclass('public.checkpoints') IS NOT NULL")).scalar()
    )


def delete_checkpoint_threads(session: Session, thread_ids: Sequence[uuid.UUID]) -> int:
    """Delete every checkpoint row of the given threads. Returns the number of rows removed."""
    if not thread_ids or not _tables_exist(session):
        return 0
    ids = [str(t) for t in thread_ids]
    removed = 0
    for table in CHECKPOINT_TABLES:
        result = session.execute(
            text(f"DELETE FROM {table} WHERE thread_id = ANY(:ids)"),  # noqa: S608
            {"ids": ids},
        )
        removed += int(getattr(result, "rowcount", 0) or 0)
    return removed


def sweep_orphan_checkpoints(session: Session) -> int:
    """Delete checkpoints whose conversation no longer exists (late writes of a cancelled run)."""
    if not _tables_exist(session):
        return 0
    removed = 0
    for table in CHECKPOINT_TABLES:
        result = session.execute(
            text(
                f"DELETE FROM {table} WHERE thread_id NOT IN "  # noqa: S608
                "(SELECT thread_id::text FROM conversations)"
            )
        )
        removed += int(getattr(result, "rowcount", 0) or 0)
    session.commit()
    if removed:
        log.info("orphan_checkpoints_removed", rows=removed)
    return removed


def purge_conversation(session: Session, conversation_id: uuid.UUID, user_id: uuid.UUID) -> str:
    """Remove checkpoints, then the conversation row (messages and executions cascade).

    Idempotent. The row goes LAST in the same transaction, so a failure leaves the tombstone for
    the reconciler to retry.
    """
    conversation = session.get(Conversation, conversation_id)
    if conversation is None:
        return "already_purged"
    if conversation.user_id != user_id:
        log.error("purge_ownership_mismatch", conversation_id=str(conversation_id))
        return "ownership_mismatch"
    if conversation.deleted_at is None:
        return "not_deleted"
    delete_checkpoint_threads(session, [conversation.thread_id])
    session.delete(conversation)
    session.commit()
    log.info("conversation_purged", conversation_id=str(conversation_id))
    return "purged"
