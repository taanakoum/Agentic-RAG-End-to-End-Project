"""End-to-end ingestion with a REAL Celery worker, PostgreSQL, Redis, Qdrant and storage.

Only the embedding model is replaced (deterministic HashEmbedder, see tests/fakes.py); OCR is the
real RapidOCR engine. One extra test uses the real FastEmbed model (marker `real_model`).
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from typing import Any

import httpx2
import pytest
from fastapi import FastAPI
from sqlalchemy import text

import app.tasks.ingestion as ingestion_tasks  # registers the tasks on the Celery app
from app.core.config import get_settings
from app.infra.container import Infrastructure
from app.ingestion import pipeline as pipeline_module
from app.ingestion import purge as purge_module
from app.ingestion.embedding import FastEmbedEmbedder
from app.ingestion.outbox import dispatch_outbox
from app.ingestion.parsing import SandboxedPdfParser
from app.ingestion.reconcile import reconcile
from app.ingestion.runtime import WorkerRuntime
from app.ingestion.store import IngestionStore
from app.services.document_service import DocumentService
from app.tasks.celery_app import celery_app
from tests.docs_formats import docx_bytes, docx_with_macro, docx_zip_bomb
from tests.fakes import HashEmbedder
from tests.integration.conftest import build_app
from tests.integration.docs_helpers import (
    API,
    new_user,
    points_for,
    scalar,
    stored_object_count,
    upload,
    wait_for_status,
)
from tests.pdfs import blank_pdf, mixed_pdf, sample_text_pdf, scanned_pdf, sectioned_pdf

pytestmark = pytest.mark.integration

BACKEND = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _worker(runtime: WorkerRuntime, embedder: HashEmbedder) -> None:
    return None


def dispatch_now(infra: Infrastructure) -> int:
    return dispatch_outbox(infra.sync_session_factory, celery_app, batch_size=100)


@pytest.fixture
def manual_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable the upload-time publish so a test controls exactly when the dispatcher runs."""

    async def noop(self: DocumentService) -> None:
        return None

    monkeypatch.setattr(DocumentService, "_kick_dispatcher", noop)


async def upload_and_wait(
    client: httpx2.AsyncClient, headers: dict[str, str], data: bytes, wanted: set[str]
) -> dict[str, Any]:
    resp = await upload(client, headers, data)
    assert resp.status_code in {200, 202}, resp.text
    return await wait_for_status(client, headers, resp.json()["document"]["id"], wanted)


def start_doomed_worker(job_id: str) -> tuple[subprocess.Popen[str], threading.Event]:
    """Start a separate OS process that begins ingesting `job_id` and hangs in the embedder."""
    env = {**os.environ, "PYTHONPATH": str(BACKEND)}
    proc = subprocess.Popen(  # noqa: S603  (our own script, fixed arguments)
        [sys.executable, str(BACKEND / "tests" / "crash_worker.py"), job_id],
        cwd=BACKEND,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    reached = threading.Event()

    def watch() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            if "EMBEDDING" in line:
                reached.set()
                return

    threading.Thread(target=watch, daemon=True).start()
    return proc, reached


# ================================================================ happy path


async def test_valid_pdf_is_ingested_end_to_end(
    client: httpx2.AsyncClient, infra: Infrastructure, embedder: HashEmbedder
) -> None:
    headers = await new_user(client)
    seen: list[str] = []

    def snapshot() -> None:  # called by the embedder, i.e. while the worker is in EMBEDDING
        with infra.sync_session_factory() as s:
            seen.append(str(s.execute(text("SELECT status FROM documents")).scalar()))

    embedder.on_dense = snapshot
    up = (await upload(client, headers, sectioned_pdf("e2e"))).json()
    doc = await wait_for_status(client, headers, up["document"]["id"], {"completed", "failed"})

    assert doc["status"] == "completed", doc
    assert seen and set(seen) == {"embedding"}  # the persisted status was truthful mid-run
    assert doc["page_count"] == 2
    assert doc["chunk_count"] and doc["chunk_count"] >= 2
    assert doc["ocr_status"] == "not_needed"
    assert doc["error"] is None
    assert doc["completed_at"] is not None
    job = (await client.get(f"{API}/ingestion/{up['job_id']}", headers=headers)).json()
    assert (job["status"], job["attempts"], job["error"]) == ("succeeded", 1, None)

    points = points_for(doc["id"])
    assert len(points) == doc["chunk_count"]
    user_id = await scalar(infra, "SELECT user_id::text FROM documents")
    for p in points:
        payload = p.payload
        assert payload["user_id"] == user_id
        assert payload["document_id"] == doc["id"]
        assert payload["document_version"] == 1
        assert payload["filename"] == "report.pdf"
        assert payload["embedding_model_version"] == "test-hash-embedder"
        assert payload["chunk_id"] == str(p.id)
        assert {"page_number", "heading", "ingestion_timestamp", "text", "chunk_index"} <= set(
            payload
        )
        assert len(p.vector["dense"]) == get_settings().embedding_dim
        assert p.vector["sparse"].indices
    assert {p.payload["page_number"] for p in points} == {1, 2}
    by_heading = {p.payload["heading"]: p.payload for p in points}
    assert by_heading["INTRODUCTION"]["page_number"] == 1
    assert by_heading["FINANCIAL SUMMARY"]["page_number"] == 2
    assert all(p.payload["section_path"] == p.payload["heading"] for p in points)
    assert any("Reference code e2e" in p.payload["text"] for p in points)
    assert doc["parser"] == "light"
    assert await scalar(infra, "SELECT count(*) FROM outbox_events WHERE sent_at IS NULL") == 0
    assert stored_object_count() == 1


async def test_vectors_are_filterable_by_owner(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    alice = await new_user(client, "alice@example.com")
    bob = await new_user(client, "bob@example.com")
    data = sample_text_pdf("both")
    a = await upload_and_wait(client, alice, data, {"completed"})
    b = await upload_and_wait(client, bob, data, {"completed"})
    assert a["id"] != b["id"]
    alice_id = await scalar(
        infra, "SELECT id::text FROM users WHERE email_normalized = 'alice@example.com'"
    )
    bob_id = await scalar(
        infra, "SELECT id::text FROM users WHERE email_normalized = 'bob@example.com'"
    )
    assert {p.payload["document_id"] for p in points_for(user_id=alice_id)} == {a["id"]}
    assert {p.payload["document_id"] for p in points_for(user_id=bob_id)} == {b["id"]}
    assert points_for(document_id=a["id"], user_id=bob_id) == []  # wrong owner finds nothing


# ================================================================ OCR


async def test_scanned_pdf_is_recovered_with_open_source_ocr(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    headers = await new_user(client)
    doc = await upload_and_wait(client, headers, scanned_pdf(), {"completed", "failed"})
    assert doc["status"] == "completed", doc
    assert doc["ocr_status"] == "used"
    assert doc["ocr_pages"] == 1
    text_chunks = " ".join(p.payload["text"] for p in points_for(doc["id"])).upper()
    assert "INVOICE" in text_chunks
    assert "1250" in text_chunks
    assert {p.payload["page_number"] for p in points_for(doc["id"])} == {1}


async def test_scanned_pdf_with_ocr_disabled_fails_with_a_clear_reason(
    client: httpx2.AsyncClient, runtime: WorkerRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    no_ocr = runtime.settings.model_copy(update={"ocr_enabled": False})
    monkeypatch.setattr(runtime, "parser", SandboxedPdfParser(no_ocr))
    headers = await new_user(client)
    doc = await upload_and_wait(client, headers, scanned_pdf(), {"failed", "completed"})
    assert doc["status"] == "failed"
    assert doc["error"]["code"] == "DOCUMENT_NO_TEXT"
    assert "OCR is disabled" in doc["error"]["message"]
    assert doc["error"]["recoverable"] is False
    assert points_for(doc["id"]) == []  # nothing meaningless was indexed


async def test_text_pdf_never_triggers_ocr_but_a_mixed_pdf_ocrs_only_the_scanned_page(
    client: httpx2.AsyncClient, runtime: WorkerRuntime
) -> None:
    headers = await new_user(client)
    text_doc = await upload_and_wait(client, headers, sample_text_pdf("noocr"), {"completed"})
    assert (text_doc["ocr_status"], text_doc["ocr_pages"]) == ("not_needed", 0)

    mixed = await upload_and_wait(client, headers, mixed_pdf(), {"completed", "failed"})
    assert mixed["status"] == "completed", mixed
    assert mixed["page_count"] == 3
    assert (mixed["ocr_status"], mixed["ocr_pages"]) == ("used", 1)  # only the scanned page
    by_page = {p.payload["page_number"]: p.payload["text"] for p in points_for(mixed["id"])}
    assert "quarterly results" in " ".join(t for n, t in by_page.items() if n in {1, 2})
    assert "INVOICE" in by_page[3].upper()


async def test_pdf_with_no_text_and_no_images_fails_permanently(client: httpx2.AsyncClient) -> None:
    headers = await new_user(client)
    doc = await upload_and_wait(client, headers, blank_pdf(), {"failed", "completed"})
    assert doc["status"] == "failed"
    assert doc["error"]["code"] == "DOCUMENT_NO_TEXT"
    assert doc["error"]["recoverable"] is False
    assert doc["ocr_status"] in {"used", "failed", "not_needed"}
    assert points_for(doc["id"]) == []
    retry = await client.post(f"{API}/ingestion/{doc['latest_job_id']}/retry", headers=headers)
    assert retry.status_code == 409  # permanent failures are not retryable


# ================================================================ idempotency and retries


async def test_duplicate_task_delivery_processes_the_job_once(
    client: httpx2.AsyncClient, infra: Infrastructure, manual_dispatch: None, embedder: HashEmbedder
) -> None:
    headers = await new_user(client)
    up = (await upload(client, headers, sample_text_pdf("dupdeliver"))).json()
    for _ in range(4):  # the same message delivered four times
        ingestion_tasks.process_document.delay(up["job_id"])
    doc = await wait_for_status(client, headers, up["document"]["id"], {"completed", "failed"})
    assert doc["status"] == "completed"
    await asyncio.sleep(1.0)  # let the redundant deliveries drain
    assert await scalar(infra, "SELECT attempts FROM ingestion_jobs") == 1
    assert len(points_for(doc["id"])) == doc["chunk_count"]
    assert embedder.calls >= 1


async def test_transient_provider_failures_are_retried_with_backoff(
    client: httpx2.AsyncClient, infra: Infrastructure, embedder: HashEmbedder
) -> None:
    headers = await new_user(client)
    embedder.fail_next = 2
    doc = await upload_and_wait(client, headers, sample_text_pdf("flaky"), {"completed", "failed"})
    assert doc["status"] == "completed"
    assert await scalar(infra, "SELECT attempts FROM ingestion_jobs") == 3
    assert len(points_for(doc["id"])) == doc["chunk_count"]


async def test_retries_exhausted_then_manual_retry_succeeds(
    client: httpx2.AsyncClient, infra: Infrastructure, embedder: HashEmbedder
) -> None:
    headers = await new_user(client)
    embedder.fail_next = 1000
    up = (await upload(client, headers, sample_text_pdf("outage"))).json()
    doc = await wait_for_status(client, headers, up["document"]["id"], {"failed", "completed"})
    assert doc["status"] == "failed"
    assert doc["error"]["code"] == "INGESTION_FAILED"
    assert doc["error"]["recoverable"] is True
    assert (
        await scalar(infra, "SELECT attempts FROM ingestion_jobs")
        == get_settings().ingestion_max_attempts
    )
    assert points_for(doc["id"]) == []

    embedder.fail_next = 0  # the provider recovers
    retry = await client.post(f"{API}/ingestion/{up['job_id']}/retry", headers=headers)
    assert retry.status_code == 202
    assert retry.json()["job_id"] != up["job_id"]
    done = await wait_for_status(client, headers, up["document"]["id"], {"completed", "failed"})
    assert done["status"] == "completed"
    assert done["error"] is None
    assert len(points_for(doc["id"])) == done["chunk_count"]
    assert await scalar(infra, "SELECT count(*) FROM ingestion_jobs") == 2
    again = await client.post(f"{API}/ingestion/{up['job_id']}/retry", headers=headers)
    assert again.status_code == 409  # already completed: nothing to retry


async def test_qdrant_failure_keeps_the_document_out_of_completed_until_it_works(
    client: httpx2.AsyncClient, infra: Infrastructure, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed: list[str] = []
    real = pipeline_module.upsert_chunks
    failures = {"left": 1}

    def flaky(*args: Any, **kwargs: Any) -> int:
        if failures["left"]:
            failures["left"] -= 1
            with infra.sync_session_factory() as s:
                observed.append(str(s.execute(text("SELECT status FROM documents")).scalar()))
            raise ConnectionError("qdrant unavailable")
        return real(*args, **kwargs)

    monkeypatch.setattr(pipeline_module, "upsert_chunks", flaky)
    headers = await new_user(client)
    doc = await upload_and_wait(client, headers, sample_text_pdf("qfail"), {"completed", "failed"})
    assert observed == ["indexing"]  # when Qdrant failed the document was INDEXING, never COMPLETED
    assert doc["status"] == "completed"
    assert len(points_for(doc["id"])) == doc["chunk_count"]


async def test_qdrant_succeeds_but_the_database_update_fails_then_recovers_idempotently(
    client: httpx2.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = IngestionStore.complete
    state = {"failed": False}

    def flaky_complete(self: IngestionStore, *args: Any, **kwargs: Any) -> None:
        if not state["failed"]:
            state["failed"] = True
            raise ConnectionError("postgres connection lost while completing")
        real(self, *args, **kwargs)

    monkeypatch.setattr(IngestionStore, "complete", flaky_complete)
    headers = await new_user(client)
    doc = await upload_and_wait(client, headers, sample_text_pdf("dbfail"), {"completed", "failed"})
    assert state["failed"] is True
    assert doc["status"] == "completed"
    # the first run already wrote every point; the retry overwrote them (deterministic ids)
    assert len(points_for(doc["id"])) == doc["chunk_count"]


# ================================================================ worker safety checks


async def test_worker_verifies_ownership_before_processing(
    client: httpx2.AsyncClient, infra: Infrastructure, manual_dispatch: None
) -> None:
    alice = await new_user(client, "alice@example.com")
    await new_user(client, "bob@example.com")
    up = (await upload(client, alice, sample_text_pdf("owner"))).json()
    async with (
        infra.session_factory() as session
    ):  # a corrupted/forged job pointing at another user
        await session.execute(
            text(
                "UPDATE ingestion_jobs SET user_id = "
                "(SELECT id FROM users WHERE email_normalized = 'bob@example.com')"
            )
        )
        await session.commit()
    dispatch_now(infra)
    doc = await wait_for_status(client, alice, up["document"]["id"], {"failed", "completed"})
    assert doc["status"] == "failed"
    assert doc["error"]["code"] == "OWNERSHIP_MISMATCH"
    assert points_for(doc["id"]) == []


async def test_worker_detects_a_tampered_stored_file(
    client: httpx2.AsyncClient, infra: Infrastructure, manual_dispatch: None
) -> None:
    headers = await new_user(client)
    up = (await upload(client, headers, sample_text_pdf("tamper"))).json()
    key = await scalar(infra, "SELECT storage_key FROM documents")
    (get_settings().storage_local_root / key).write_bytes(sample_text_pdf("different content"))
    dispatch_now(infra)
    doc = await wait_for_status(client, headers, up["document"]["id"], {"failed", "completed"})
    assert doc["status"] == "failed"
    assert doc["error"]["code"] == "STORAGE_INTEGRITY"
    assert points_for(doc["id"]) == []


# ================================================================ outbox / crash recovery


async def test_document_saved_but_task_never_published_is_picked_up_by_the_dispatcher(
    client: httpx2.AsyncClient, infra: Infrastructure, manual_dispatch: None
) -> None:
    headers = await new_user(client)
    up = (await upload(client, headers, sample_text_pdf("unpublished"))).json()
    await asyncio.sleep(1.0)
    still = (await client.get(f"{API}/documents/{up['document']['id']}", headers=headers)).json()
    assert still["status"] == "queued"  # saved, but nothing was published
    assert await scalar(infra, "SELECT count(*) FROM outbox_events WHERE sent_at IS NULL") == 1
    assert dispatch_now(infra) == 1  # what Celery Beat does every few seconds
    done = await wait_for_status(client, headers, up["document"]["id"], {"completed", "failed"})
    assert done["status"] == "completed"


async def test_worker_process_killed_mid_ingest_is_recovered_by_the_reconciler(
    client: httpx2.AsyncClient, infra: Infrastructure, runtime: WorkerRuntime, manual_dispatch: None
) -> None:
    headers = await new_user(client)
    up = (await upload(client, headers, sample_text_pdf("killed"))).json()
    doc_id = up["document"]["id"]
    # consume the original outbox event ourselves; the doomed worker process runs the job directly
    async with infra.session_factory() as session:
        await session.execute(text("UPDATE outbox_events SET sent_at = now()"))
        await session.commit()
    proc, reached = start_doomed_worker(up["job_id"])
    try:
        assert reached.wait(timeout=60), "the doomed worker never reached the embedding stage"
    finally:
        proc.kill()  # hard kill: no cleanup, no failure handling, like a lost container
        proc.wait(timeout=30)

    stuck = (await client.get(f"{API}/documents/{doc_id}", headers=headers)).json()
    assert stuck["status"] == "embedding"  # the crash left the persisted state mid-pipeline
    assert await scalar(infra, "SELECT status FROM ingestion_jobs") == "processing"

    async with infra.session_factory() as session:
        await session.execute(
            text("UPDATE ingestion_jobs SET updated_at = now() - interval '5 minutes'")
        )
        await session.commit()
    with infra.sync_session_factory() as session:
        report = reconcile(session, get_settings())
    assert report.recovered_processing == 1
    assert dispatch_now(infra) >= 1
    done = await wait_for_status(client, headers, doc_id, {"completed", "failed"})
    assert done["status"] == "completed"
    assert await scalar(infra, "SELECT attempts FROM ingestion_jobs") == 2
    assert len(points_for(doc_id)) == done["chunk_count"]


# ================================================================ deletion and re-index


async def test_delete_removes_vectors_files_and_rows_but_only_for_that_document(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    headers = await new_user(client)
    keep = await upload_and_wait(client, headers, sample_text_pdf("keep"), {"completed"})
    gone = await upload_and_wait(client, headers, sample_text_pdf("gone"), {"completed"})
    assert stored_object_count() == 2
    assert (
        await client.delete(f"{API}/documents/{gone['id']}", headers=headers)
    ).status_code == 202
    for _ in range(100):  # wait for the asynchronous purge
        if await scalar(infra, "SELECT count(*) FROM documents") == 1:
            break
        await asyncio.sleep(0.2)
    assert await scalar(infra, "SELECT count(*) FROM documents") == 1
    assert points_for(gone["id"]) == []
    assert len(points_for(keep["id"])) == keep["chunk_count"]
    assert stored_object_count() == 1
    assert await scalar(infra, "SELECT count(*) FROM ingestion_jobs") == 1
    assert (await client.get(f"{API}/documents/{keep['id']}", headers=headers)).status_code == 200


async def test_purge_is_retried_when_qdrant_deletion_fails(
    client: httpx2.AsyncClient, infra: Infrastructure, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = await new_user(client)
    doc = await upload_and_wait(client, headers, sample_text_pdf("purgeretry"), {"completed"})
    real = purge_module.delete_document_points
    attempts = {"n": 0}

    def flaky(*args: Any, **kwargs: Any) -> None:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise ConnectionError("qdrant unavailable")
        real(*args, **kwargs)

    monkeypatch.setattr(purge_module, "delete_document_points", flaky)
    await client.delete(f"{API}/documents/{doc['id']}", headers=headers)
    for _ in range(100):
        if await scalar(infra, "SELECT count(*) FROM documents") == 0:
            break
        await asyncio.sleep(0.2)
    assert attempts["n"] >= 2
    assert await scalar(infra, "SELECT count(*) FROM documents") == 0
    assert points_for(doc["id"]) == []
    assert stored_object_count() == 0


async def test_delete_while_processing_leaves_no_orphans(
    client: httpx2.AsyncClient, infra: Infrastructure, embedder: HashEmbedder
) -> None:
    headers = await new_user(client)
    fired = threading.Event()

    def delete_mid_flight() -> None:
        if fired.is_set():
            return
        fired.set()
        with infra.sync_session_factory() as s:  # the user deletes while the worker is embedding
            s.execute(text("UPDATE documents SET deleted_at = now()"))
            s.execute(
                text(
                    "INSERT INTO outbox_events (kind, payload) SELECT 'purge_document', "
                    "jsonb_build_object('document_id', id::text, 'user_id', user_id::text) "
                    "FROM documents"
                )
            )
            s.commit()

    embedder.on_dense = delete_mid_flight
    up = (await upload(client, headers, sample_text_pdf("race-delete"))).json()
    for _ in range(150):
        if fired.is_set() and await scalar(infra, "SELECT count(*) FROM documents") == 0:
            break
        dispatch_now(infra)
        await asyncio.sleep(0.2)
    assert fired.is_set()
    assert await scalar(infra, "SELECT count(*) FROM documents") == 0
    assert points_for(up["document"]["id"]) == []
    assert stored_object_count() == 0


async def test_delete_during_indexing_removes_the_points_the_worker_just_wrote(
    client: httpx2.AsyncClient, infra: Infrastructure, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = pipeline_module.upsert_chunks

    def upsert_then_delete(*args: Any, **kwargs: Any) -> int:
        count = real(*args, **kwargs)
        with infra.sync_session_factory() as s:  # deleted right after the vectors were written
            s.execute(text("UPDATE documents SET deleted_at = now()"))
            s.commit()
        return count

    monkeypatch.setattr(pipeline_module, "upsert_chunks", upsert_then_delete)
    headers = await new_user(client)
    up = (await upload(client, headers, sample_text_pdf("late-delete"))).json()
    for _ in range(100):
        if await scalar(infra, "SELECT status FROM ingestion_jobs") == "superseded":
            break
        await asyncio.sleep(0.2)
    assert await scalar(infra, "SELECT status FROM ingestion_jobs") == "superseded"
    assert points_for(up["document"]["id"]) == []  # the worker cleaned up after itself


async def test_reindex_creates_a_new_version_and_replaces_the_old_vectors(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    headers = await new_user(client)
    first = await upload_and_wait(client, headers, sample_text_pdf("v1"), {"completed"})
    resp = await client.post(f"{API}/documents/{first['id']}/reindex", headers=headers)
    assert resp.status_code == 202
    doc: dict[str, Any] = {}
    for _ in range(100):
        doc = (await client.get(f"{API}/documents/{first['id']}", headers=headers)).json()
        if doc["status"] == "completed" and doc["version"] == 2:
            break
        await asyncio.sleep(0.2)
    assert doc["version"] == 2
    assert doc["status"] == "completed"
    points = points_for(first["id"])
    assert len(points) == doc["chunk_count"]
    assert {p.payload["document_version"] for p in points} == {2}  # version 1 points are gone
    assert await scalar(infra, "SELECT count(*) FROM ingestion_jobs") == 2


# ================================================================ restart persistence


async def test_everything_survives_a_restart_of_the_api_and_its_connections(
    client: httpx2.AsyncClient,
) -> None:
    headers = await new_user(client)
    doc = await upload_and_wait(client, headers, sample_text_pdf("restart"), {"completed"})

    # Build every client from scratch (new DB pools, Redis, Qdrant, storage), as a restarted
    # container would. Nothing in memory from the first app is reused.
    fresh_infra = Infrastructure.create(get_settings())
    fresh_runtime = WorkerRuntime.create(get_settings())
    try:
        app: FastAPI = build_app(get_settings(), fresh_infra)
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://test") as c2:
            again = await c2.get(f"{API}/documents/{doc['id']}", headers=headers)
        assert again.status_code == 200
        assert again.json()["status"] == "completed"
        assert again.json()["chunk_count"] == doc["chunk_count"]
        assert len(points_for(doc["id"])) == doc["chunk_count"]  # Qdrant data persisted
        key = await scalar(fresh_infra, "SELECT storage_key FROM documents")
        with fresh_runtime.storage.open(key) as handle:  # object storage persisted
            assert handle.read(5) == b"%PDF-"
    finally:
        await fresh_infra.close()
        fresh_runtime.close()


# ================================================================ real embedding model


@pytest.mark.real_model
def test_real_fastembed_models_produce_expected_vectors() -> None:
    """Uses the real BAAI/bge-small-en-v1.5 and Qdrant/bm25 models (downloaded on first use)."""
    embedder = FastEmbedEmbedder(get_settings())
    a, b, c = embedder.embed_dense(
        [
            "Revenue grew steadily across all regions this quarter.",
            "Sales increased in every region during the quarter.",
            "The cat sat on the mat and watched the birds outside.",
        ]
    )
    assert len(a) == get_settings().embedding_dim

    def cos(x: list[float], y: list[float]) -> float:
        dot = sum(i * j for i, j in zip(x, y, strict=True))
        return dot / ((sum(i * i for i in x) ** 0.5) * (sum(j * j for j in y) ** 0.5))

    assert cos(a, b) > cos(a, c)  # semantically close sentences are closer than unrelated ones
    sparse = embedder.embed_sparse(["Revenue grew steadily across all regions."])[0]
    assert sparse.indices and len(sparse.indices) == len(sparse.values)
    assert all(v > 0 for v in sparse.values)


@pytest.mark.real_model
async def test_ingestion_with_the_real_embedding_model(
    client: httpx2.AsyncClient, runtime: WorkerRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtime, "embedder", FastEmbedEmbedder(runtime.settings))
    headers = await new_user(client)
    doc = await upload_and_wait(client, headers, sample_text_pdf("real"), {"completed", "failed"})
    assert doc["status"] == "completed", doc
    points = points_for(doc["id"])
    assert points
    assert all(len(p.vector["dense"]) == 384 for p in points)
    assert all(p.payload["embedding_model_version"] == "BAAI/bge-small-en-v1.5" for p in points)
    assert uuid.UUID(points[0].payload["chunk_id"])


# ================================================================ other formats: TXT, MD, CSV, DOCX


async def upload_named(
    client: httpx2.AsyncClient,
    headers: dict[str, str],
    data: bytes,
    name: str,
    mime: str | None,
) -> dict[str, Any]:
    resp = await upload(client, headers, data, filename=name, content_type=mime)
    assert resp.status_code == 202, resp.text
    return await wait_for_status(
        client, headers, resp.json()["document"]["id"], {"completed", "failed"}
    )


async def test_markdown_document_is_indexed_with_its_section_paths(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    headers = await new_user(client)
    md = (
        "# Operations Guide\n\n## Upload limits\n\n"
        + "Files above twenty five megabytes are rejected by the gateway. " * 6
        + "\n\n## Support hours\n\n"
        + "The help desk answers tickets between eight and eighteen on weekdays. " * 6
    )
    doc = await upload_named(client, headers, md.encode(), "guide.md", "text/markdown")
    assert doc["status"] == "completed", doc
    assert (doc["source_format"], doc["parser"], doc["ocr_status"]) == (
        "md",
        "structured",
        "not_needed",
    )
    paths = {p.payload["section_path"] for p in points_for(doc["id"])}
    assert "Operations Guide > Upload limits" in paths
    assert "Operations Guide > Support hours" in paths
    key = await scalar(infra, "SELECT storage_key FROM documents")
    assert key.endswith("/original.md")


async def test_csv_rows_are_indexed_whole_with_their_column_names(
    client: httpx2.AsyncClient,
) -> None:
    headers = await new_user(client)
    lines = ["city,population,country"] + [f"City{i},{i * 1000},Land{i % 7}" for i in range(400)]
    doc = await upload_named(client, headers, "\n".join(lines).encode(), "cities.csv", "text/csv")
    assert doc["status"] == "completed", doc
    assert doc["source_format"] == "csv"
    texts = "\n".join(p.payload["text"] for p in points_for(doc["id"]))
    for i in (0, 199, 399):
        assert f"city: City{i}; population: {i * 1000}; country: Land{i % 7}" in texts
    assert doc["chunk_count"] > 3


async def test_docx_document_is_indexed_with_headings_and_table_rows(
    client: httpx2.AsyncClient,
) -> None:
    headers = await new_user(client)
    mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    doc = await upload_named(client, headers, docx_bytes(), "report.docx", mime)
    assert doc["status"] == "completed", doc
    assert (doc["source_format"], doc["parser"]) == ("docx", "structured")
    texts = "\n".join(p.payload["text"] for p in points_for(doc["id"]))
    assert "Region: North; Revenue: 4.2M; Growth: 12%" in texts
    assert "Management expects growth" in texts


async def test_plain_text_document_is_indexed(client: httpx2.AsyncClient) -> None:
    headers = await new_user(client)
    text = (
        "INTRODUCTION\n\n" + "The committee reviewed every proposal carefully before deciding. " * 8
    )
    doc = await upload_named(client, headers, text.encode(), "minutes.txt", "text/plain")
    assert doc["status"] == "completed", doc
    assert doc["source_format"] == "txt"
    assert {p.payload["heading"] for p in points_for(doc["id"])} == {"INTRODUCTION"}


async def test_invalid_non_pdf_uploads_are_rejected_without_side_effects(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    headers = await new_user(client)
    cases = [
        (b"caf\xe9", "latin.txt", "DOCUMENT_INVALID_ENCODING", 400),
        (b"plain text", "fake.docx", "DOCUMENT_INVALID_FORMAT", 400),
        (docx_with_macro(), "macro.docx", "DOCUMENT_UNSAFE", 400),
        (docx_zip_bomb(), "bomb.docx", "DOCUMENT_UNSAFE", 400),
        (b"anything", "tool.exe", "DOCUMENT_UNSUPPORTED_TYPE", 415),
    ]
    for data, name, code, status in cases:
        resp = await upload(client, headers, data, filename=name, content_type=None)
        assert resp.status_code == status, (name, resp.text)
        assert resp.json()["error"]["code"] == code
    assert await scalar(infra, "SELECT count(*) FROM documents") == 0
    assert stored_object_count() == 0


# ================================================================ Docling pipeline (slow)


@pytest.fixture
def docling_parser(runtime: WorkerRuntime, monkeypatch: pytest.MonkeyPatch) -> None:
    docling = runtime.settings.model_copy(update={"pdf_pipeline": "docling"})
    monkeypatch.setattr(runtime, "parser", SandboxedPdfParser(docling))


@pytest.mark.docling
async def test_docling_detects_layout_headings_the_text_heuristic_cannot_see(
    client: httpx2.AsyncClient, docling_parser: None
) -> None:
    from evaluation.chunking.corpus import DOCS, build_pages, render_pdf

    source = next(d for d in DOCS if d.name == "platform_guide")
    headers = await new_user(client)
    doc = await upload_and_wait(
        client, headers, render_pdf(build_pages(source)), {"completed", "failed"}
    )
    assert doc["status"] == "completed", doc
    assert doc["parser"] == "docling"
    assert (doc["ocr_status"], doc["ocr_pages"]) == ("not_needed", 0)  # text PDF: no OCR at all
    points = points_for(doc["id"])
    paths = {p.payload["section_path"] for p in points}
    # "Upload size limits" is a bold sentence-case heading: only layout analysis finds it
    assert any("Upload size limits" in path for path in paths), paths
    fact = next(p for p in points if "25 megabytes" in " ".join(p.payload["text"].split()))
    assert "Upload size limits" in fact.payload["section_path"]
    assert fact.payload["page_number"] >= 1


@pytest.mark.docling
async def test_docling_recovers_scanned_text_even_when_it_is_labelled_a_heading(
    client: httpx2.AsyncClient, docling_parser: None
) -> None:
    headers = await new_user(client)
    doc = await upload_and_wait(client, headers, scanned_pdf(), {"completed", "failed"})
    assert doc["status"] == "completed", doc
    assert (doc["parser"], doc["ocr_status"], doc["ocr_pages"]) == ("docling", "used", 1)
    text_chunks = " ".join(p.payload["text"] for p in points_for(doc["id"])).upper()
    assert "INVOICE" in text_chunks
    assert "1250" in text_chunks


@pytest.mark.docling
async def test_docling_on_a_mixed_pdf_ocrs_only_because_a_page_needs_it(
    client: httpx2.AsyncClient, docling_parser: None
) -> None:
    headers = await new_user(client)
    doc = await upload_and_wait(client, headers, mixed_pdf(), {"completed", "failed"})
    assert doc["status"] == "completed", doc
    assert doc["page_count"] == 3
    assert (doc["ocr_status"], doc["ocr_pages"]) == ("used", 1)
    texts = " ".join(p.payload["text"] for p in points_for(doc["id"])).upper()
    assert "QUARTERLY RESULTS" in texts
    assert "INVOICE" in texts
