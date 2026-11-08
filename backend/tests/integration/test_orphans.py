"""Orphan objects: written to storage but never referenced by a committed document row."""

from __future__ import annotations

import io
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx2
import pytest

from app.core.config import get_settings
from app.infra.container import Infrastructure
from app.ingestion.orphans import OrphanReport, sweep_orphans
from app.storage.factory import create_storage
from tests.integration.docs_helpers import API, new_user, scalar, stored_object_count, upload
from tests.pdfs import sample_text_pdf

pytestmark = pytest.mark.integration

BACKEND = Path(__file__).resolve().parents[2]


def key_for(user: uuid.UUID | None = None, doc: uuid.UUID | None = None) -> str:
    return f"{user or uuid.uuid4()}/{doc or uuid.uuid4()}/original.pdf"


def age_file(key: str, seconds: int) -> None:
    path = get_settings().storage_local_root / key
    old = time.time() - seconds
    os.utime(path, (old, old))


def run_crashing_upload(key: str) -> subprocess.CompletedProcess[bytes]:
    env = {**os.environ, "PYTHONPATH": str(BACKEND)}
    return subprocess.run(  # noqa: S603  (our own script, fixed arguments)
        [sys.executable, str(BACKEND / "tests" / "crash_upload.py"), key],
        cwd=BACKEND,
        env=env,
        capture_output=True,
        timeout=60,
        check=False,
    )


def sweep(infra: Infrastructure, **overrides: int) -> OrphanReport:
    settings = get_settings().model_copy(update=overrides)
    storage = create_storage(settings)
    with infra.sync_session_factory() as session:
        return sweep_orphans(session, storage, settings)


async def test_process_killed_between_put_and_commit_leaves_an_orphan_the_sweep_removes(
    infra: Infrastructure,
) -> None:
    key = key_for()
    proc = run_crashing_upload(key)
    assert proc.returncode == 1  # it really died
    assert stored_object_count() == 1  # the orphan exists
    assert await scalar(infra, "SELECT count(*) FROM documents") == 0  # and nothing references it

    # inside the grace period the object is left alone (it could be an upload in flight)
    report = sweep(infra, orphan_grace_s=3600)
    assert (report.orphans_deleted, report.skipped_young) == (0, 1)
    assert stored_object_count() == 1

    age_file(key, 7200)
    report = sweep(infra, orphan_grace_s=3600)
    assert report.orphans_deleted == 1
    assert stored_object_count() == 0


async def test_sweep_keeps_everything_that_is_referenced_or_not_ours(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    headers = await new_user(client)
    live = (await upload(client, headers, sample_text_pdf("live"))).json()["document"]["id"]
    tomb = (await upload(client, headers, sample_text_pdf("tomb"))).json()["document"]["id"]
    await client.delete(f"{API}/documents/{tomb}", headers=headers)  # tombstone, purge pending
    storage = create_storage(get_settings())
    orphan = key_for()
    foreign = "someone-elses/notes.txt"  # not our key layout: must never be touched
    young_orphan = key_for()
    for k in (orphan, foreign, young_orphan):
        storage.put(k, io.BytesIO(b"data"))
    keys = [
        await scalar(infra, "SELECT storage_key FROM documents WHERE id = :i", i=uuid.UUID(d))
        for d in (live, tomb)
    ]
    for k in (*keys, orphan, foreign):
        age_file(k, 7200)  # everything old except `young_orphan`

    report = sweep(infra, orphan_grace_s=3600)
    assert report.orphans_deleted == 1
    assert report.skipped_unknown_layout == 1
    assert not storage.exists(orphan)
    assert storage.exists(foreign)  # unknown layout: untouched
    assert storage.exists(young_orphan)  # too young to judge
    assert storage.exists(keys[0])  # referenced by a live document
    assert storage.exists(keys[1])  # referenced by a tombstoned document awaiting its purge


async def test_sweep_cleans_stale_temporary_files_and_readiness_probe_leftovers(
    infra: Infrastructure,
) -> None:
    root = get_settings().storage_local_root
    stale = root / ".tmp" / "tmp-stale"
    fresh = root / ".tmp" / "tmp-fresh"
    stale.write_bytes(b"partial write")
    fresh.write_bytes(b"in progress")
    old = time.time() - 7200
    os.utime(stale, (old, old))
    storage = create_storage(get_settings())
    probe = f"_health/{uuid.uuid4().hex}"
    storage.put(probe, io.BytesIO(b"ok"))
    age_file(probe, 7200)

    report = sweep(infra, orphan_grace_s=3600)
    assert report.temp_cleaned == 1
    assert not stale.exists()
    assert fresh.exists()
    assert not storage.exists(probe)


async def test_sweep_is_idempotent_and_survives_an_empty_store(infra: Infrastructure) -> None:
    first = sweep(infra)
    second = sweep(infra)
    assert first == second
    assert first.scanned == 0


def test_sweep_is_scheduled_by_beat() -> None:
    from app.tasks.celery_app import celery_app

    tasks = {entry["task"] for entry in celery_app.conf.beat_schedule.values()}
    assert "ingestion.sweep_orphans" in tasks
