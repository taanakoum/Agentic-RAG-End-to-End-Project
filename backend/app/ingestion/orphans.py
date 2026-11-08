"""Find and delete stored objects that no document row refers to (crash between put and commit).

An upload writes the object first and commits the row second. If the process dies in between, the
object is an orphan. The sweep lists storage, and deletes an object only when ALL hold:
  * its key has the generated layout `<user_uuid>/<document_uuid>/original.pdf`
    (anything else is never touched),
  * it is older than the grace period (so an upload in flight right now is never deleted),
  * no document row, including tombstoned ones still awaiting purge, uses that storage key.
Readiness-probe leftovers (`_health/`) and stale partial writes are cleaned too.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.logging import get_logger
from app.models.document import Document
from app.storage.base import ObjectStorage

log = get_logger(__name__)

_KEY = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/original\.pdf$"
)
_BATCH = 500


@dataclass(frozen=True, slots=True)
class OrphanReport:
    scanned: int = 0
    orphans_deleted: int = 0
    skipped_young: int = 0
    skipped_unknown_layout: int = 0
    temp_cleaned: int = 0


def sweep_orphans(session: Session, storage: ObjectStorage, settings: Settings) -> OrphanReport:
    cutoff = datetime.now(UTC) - timedelta(seconds=settings.orphan_grace_s)
    scanned = deleted = young = unknown = 0
    batch: list[str] = []

    def flush() -> int:
        if not batch:
            return 0
        known = set(
            session.execute(select(Document.storage_key).where(Document.storage_key.in_(batch)))
            .scalars()
            .all()
        )
        count = 0
        for key in batch:
            if key not in known and storage.delete(key):
                count += 1
                log.warning("orphan_object_deleted", key=key)
        batch.clear()
        return count

    for info in storage.iter_objects():
        scanned += 1
        if info.key.startswith("_health/"):
            if info.modified_at < cutoff:
                storage.delete(info.key)
            continue
        if not _KEY.fullmatch(info.key):
            unknown += 1  # not our layout: never delete what we did not create
            continue
        if info.modified_at >= cutoff:
            young += 1
            continue
        batch.append(info.key)
        if len(batch) >= _BATCH:
            deleted += flush()
    deleted += flush()
    temp = storage.sweep_temporary(settings.orphan_grace_s)
    report = OrphanReport(scanned, deleted, young, unknown, temp)
    if deleted or temp:
        log.info("orphan_sweep_done", deleted=deleted, temp_cleaned=temp, scanned=scanned)
    return report
