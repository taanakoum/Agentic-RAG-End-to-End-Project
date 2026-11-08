"""Document API: validation, dedup, ownership, tombstoning, outbox, reconciler (no worker)."""

from __future__ import annotations

import asyncio
import threading
import uuid
from datetime import timedelta
from typing import Any, cast

import httpx2
import pytest
from celery import Celery
from fastapi import FastAPI
from sqlalchemy import text

from app.core.config import get_settings
from app.infra.container import Infrastructure
from app.ingestion.outbox import KIND_INGEST, dispatch_outbox
from app.ingestion.reconcile import reconcile
from app.ingestion.states import DocumentStatus
from app.ingestion.store import IngestionStore, LostStateError, utcnow
from app.services.document_service import DocumentService
from tests.integration.docs_helpers import (
    API,
    bearer,
    new_user,
    scalar,
    stored_object_count,
    upload,
)
from tests.pdfs import (
    corrupted_pdf,
    encrypted_pdf,
    make_pdf,
    sample_text_pdf,
    truncated_pdf,
)

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def no_dispatch_kick(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests inspect the state BEFORE any dispatcher/worker touches it."""

    async def noop(self: DocumentService) -> None:
        return None

    monkeypatch.setattr(DocumentService, "_kick_dispatcher", noop)


async def counts(infra: Infrastructure) -> tuple[int, int, int]:
    return (
        await scalar(infra, "SELECT count(*) FROM documents"),
        await scalar(infra, "SELECT count(*) FROM ingestion_jobs"),
        await scalar(infra, "SELECT count(*) FROM outbox_events"),
    )


# ================================================================ rejected uploads


async def test_upload_requires_authentication(client: httpx2.AsyncClient) -> None:
    resp = await client.post(
        f"{API}/documents/upload", files={"file": ("a.pdf", sample_text_pdf(), "application/pdf")}
    )
    assert resp.status_code == 401


@pytest.mark.parametrize(
    ("label", "data", "kwargs", "status", "code"),
    [
        ("empty file", b"", {}, 400, "DOCUMENT_EMPTY"),
        (
            "renamed non-pdf",
            b"plain text, definitely not a pdf",
            {},
            400,
            "DOCUMENT_INVALID_FORMAT",
        ),
        ("corrupted", corrupted_pdf(), {}, 400, "DOCUMENT_CORRUPTED"),
        ("truncated", truncated_pdf(), {}, 400, "DOCUMENT_CORRUPTED"),
        ("encrypted", encrypted_pdf(), {}, 422, "DOCUMENT_ENCRYPTED"),
        (
            "wrong extension",
            sample_text_pdf(),
            {"filename": "report.txt"},
            415,
            "DOCUMENT_UNSUPPORTED_TYPE",
        ),
        (
            "wrong mime",
            sample_text_pdf(),
            {"content_type": "text/plain"},
            415,
            "DOCUMENT_UNSUPPORTED_TYPE",
        ),
    ],
)
async def test_invalid_uploads_are_rejected_without_side_effects(
    client: httpx2.AsyncClient,
    infra: Infrastructure,
    label: str,
    data: bytes,
    kwargs: dict[str, Any],
    status: int,
    code: str,
) -> None:
    headers = await new_user(client)
    resp = await upload(client, headers, data, **kwargs)
    assert resp.status_code == status, (label, resp.text)
    error = resp.json()["error"]
    assert error["code"] == code
    assert error["request_id"] == resp.headers["x-request-id"]
    assert await counts(infra) == (0, 0, 0)
    assert stored_object_count() == 0


async def test_missing_file_field_is_a_validation_error(client: httpx2.AsyncClient) -> None:
    headers = await new_user(client)
    resp = await client.post(f"{API}/documents/upload", data={"other": "x"}, headers=headers)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
    assert resp.json()["error"]["details"][0]["field"] == "file"
    no_multipart = await client.post(f"{API}/documents/upload", json={}, headers=headers)
    assert no_multipart.status_code == 422


async def test_oversized_pdf_and_too_many_pages(
    client_with_settings: Any, infra: Infrastructure
) -> None:
    small = await client_with_settings(get_settings().model_copy(update={"max_pdf_bytes": 2048}))
    headers = await new_user(small)
    big = await upload(small, headers, make_pdf(["lots of text " * 200]))
    assert big.status_code == 413
    assert big.json()["error"]["code"] == "DOCUMENT_TOO_LARGE"

    few = await client_with_settings(get_settings().model_copy(update={"max_pdf_pages": 2}))
    headers = bearer(
        (
            await few.post(
                f"{API}/auth/login",
                json={"email": "alice@example.com", "password": "correct-horse-battery-staple"},
            )
        ).json()["access_token"]
    )
    many = await upload(
        few, headers, make_pdf([f"page {i} with some text in it" for i in range(3)])
    )
    assert many.status_code == 422
    assert many.json()["error"]["code"] == "DOCUMENT_TOO_MANY_PAGES"
    assert await counts(infra) == (0, 0, 0)
    assert stored_object_count() == 0


async def test_request_body_over_the_upload_limit_is_413(client_with_settings: Any) -> None:
    settings = get_settings().model_copy(update={"max_pdf_bytes": 4096})
    c = await client_with_settings(settings)
    headers = await new_user(c)
    # the app-level limit is max_pdf_bytes + 1 MiB of multipart overhead
    resp = await upload(c, headers, b"%PDF-1.4\n" + b"0" * (4096 + 1024 * 1024 + 10))
    assert resp.status_code == 413
    assert resp.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"


async def test_upload_rate_limit(client_with_settings: Any) -> None:
    c = await client_with_settings(get_settings().model_copy(update={"rl_upload_limit": 2}))
    headers = await new_user(c)
    codes = [(await upload(c, headers, b"")).status_code for _ in range(3)]  # all rejected (empty)
    assert codes == [400, 400, 429]


# ================================================================ accepted uploads


async def test_valid_upload_creates_document_job_and_outbox_atomically(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    headers = await new_user(client)
    resp = await upload(client, headers, sample_text_pdf(), filename="../../Quarterly Report.pdf")
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["duplicate"] is False
    doc = body["document"]
    assert doc["status"] == "queued"
    assert doc["filename"] == "Quarterly Report.pdf"  # path components stripped
    assert doc["page_count"] == 2
    assert doc["latest_job_id"] == body["job_id"]
    assert doc["error"] is None
    # internals never leak
    assert "storage_key" not in resp.text
    assert "content_hash" not in resp.text
    assert "user_id" not in resp.text

    assert await counts(infra) == (1, 1, 1)
    assert await scalar(infra, "SELECT status FROM ingestion_jobs") == "queued"
    assert await scalar(infra, "SELECT sent_at IS NULL FROM outbox_events") is True
    assert await scalar(infra, "SELECT payload->>'job_id' FROM outbox_events") == body["job_id"]
    assert stored_object_count() == 1
    key = await scalar(infra, "SELECT storage_key FROM documents")
    assert key.endswith("/original.pdf")
    assert "Quarterly" not in key  # keys are generated, never derived from the filename


async def test_duplicate_upload_returns_the_existing_document(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    headers = await new_user(client)
    data = sample_text_pdf("dup")
    first = await upload(client, headers, data)
    second = await upload(client, headers, data, filename="renamed.pdf")
    assert (first.status_code, second.status_code) == (202, 200)
    assert second.json()["duplicate"] is True
    assert second.json()["document"]["id"] == first.json()["document"]["id"]
    assert second.json()["job_id"] == first.json()["job_id"]
    assert await counts(infra) == (1, 1, 1)
    assert stored_object_count() == 1  # the redundant copy was removed


async def test_concurrent_identical_uploads_yield_exactly_one_document(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    headers = await new_user(client)
    data = sample_text_pdf("race")
    responses = await asyncio.gather(*(upload(client, headers, data) for _ in range(12)))
    codes = sorted(r.status_code for r in responses)
    assert codes.count(202) == 1, codes
    assert codes.count(200) == 11, codes
    assert len({r.json()["document"]["id"] for r in responses}) == 1
    assert await counts(infra) == (1, 1, 1)
    assert stored_object_count() == 1  # losers deleted their own copies


async def test_same_file_from_different_users_is_independent_and_not_revealed(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    alice = await new_user(client, "alice@example.com")
    bob = await new_user(client, "bob@example.com")
    data = sample_text_pdf("shared")
    a = await upload(client, alice, data)
    b = await upload(client, bob, data)
    # Bob gets a normal 202 for his own copy: nothing reveals that Alice uploaded it.
    assert (a.status_code, b.status_code) == (202, 202)
    assert b.json()["duplicate"] is False
    assert a.json()["document"]["id"] != b.json()["document"]["id"]
    assert (await counts(infra))[0] == 2
    assert stored_object_count() == 2


async def test_failed_commit_does_not_leave_an_orphan_object(
    client: httpx2.AsyncClient,
    app: FastAPI,
    infra: Infrastructure,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.repositories.documents import DocumentRepository

    async def boom(self: DocumentRepository, **kwargs: Any) -> None:
        raise RuntimeError("database exploded")

    headers = await new_user(client)
    monkeypatch.setattr(DocumentRepository, "insert_or_get", boom)
    transport = httpx2.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as c2:
        resp = await upload(c2, headers, sample_text_pdf("orphan"))
    assert resp.status_code == 500
    assert resp.json()["error"]["code"] == "INTERNAL_ERROR"
    assert stored_object_count() == 0
    assert (await counts(infra))[0] == 0


# ================================================================ reads and ownership


async def test_list_get_status_and_pagination(client: httpx2.AsyncClient) -> None:
    headers = await new_user(client)
    ids: list[str] = []
    for i in range(3):
        r = await upload(client, headers, sample_text_pdf(f"doc{i}"), filename=f"d{i}.pdf")
        ids.append(r.json()["document"]["id"])
    page1 = (await client.get(f"{API}/documents?limit=2", headers=headers)).json()
    page2 = (await client.get(f"{API}/documents?limit=2&offset=2", headers=headers)).json()
    assert (page1["total"], len(page1["items"]), len(page2["items"])) == (3, 2, 1)
    assert {d["id"] for d in page1["items"] + page2["items"]} == set(ids)
    queued = (await client.get(f"{API}/documents?status=queued", headers=headers)).json()
    assert queued["total"] == 3
    assert (await client.get(f"{API}/documents?status=completed", headers=headers)).json()[
        "total"
    ] == 0
    for path in (f"documents/{ids[0]}", f"documents/{ids[0]}/status"):
        r = await client.get(f"{API}/{path}", headers=headers)
        assert r.status_code == 200
        assert r.json()["id"] == ids[0]
    assert (await client.get(f"{API}/documents?limit=0", headers=headers)).status_code == 422
    assert (await client.get(f"{API}/documents/not-a-uuid", headers=headers)).status_code == 422


async def test_users_cannot_see_or_touch_each_others_documents(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    alice = await new_user(client, "alice@example.com")
    bob = await new_user(client, "bob@example.com")
    up = (await upload(client, alice, sample_text_pdf("private"))).json()
    doc_id, job_id = up["document"]["id"], up["job_id"]
    ghost = str(uuid.uuid4())

    probes = [
        ("GET", "documents/{}", None),
        ("GET", "documents/{}/status", None),
        ("DELETE", "documents/{}", None),
        ("POST", "documents/{}/reindex", None),
    ]
    for method, template, _ in probes:
        real = await client.request(method, f"{API}/{template.format(doc_id)}", headers=bob)
        fake = await client.request(method, f"{API}/{template.format(ghost)}", headers=bob)
        assert real.status_code == fake.status_code == 404, (method, template)
        r, f = real.json()["error"], fake.json()["error"]
        assert (r["code"], r["message"], r["details"]) == (f["code"], f["message"], f["details"])
    for method, path in (("GET", f"ingestion/{job_id}"), ("POST", f"ingestion/{job_id}/retry")):
        real = await client.request(method, f"{API}/{path}", headers=bob)
        fake = await client.request(method, f"{API}/{path.replace(job_id, ghost)}", headers=bob)
        assert real.status_code == fake.status_code == 404

    assert (await client.get(f"{API}/documents", headers=bob)).json() == {
        "items": [],
        "total": 0,
        "limit": 20,
        "offset": 0,
    }
    # Alice's document is untouched by all of that (not tombstoned, not requeued).
    assert await scalar(infra, "SELECT deleted_at IS NULL FROM documents") is True
    assert (await client.get(f"{API}/documents/{doc_id}", headers=alice)).status_code == 200
    assert (
        await scalar(infra, "SELECT count(*) FROM outbox_events WHERE kind = 'purge_document'") == 0
    )


async def test_unauthenticated_access_to_every_document_route(client: httpx2.AsyncClient) -> None:
    some = str(uuid.uuid4())
    for method, path in [
        ("GET", "documents"),
        ("GET", f"documents/{some}"),
        ("GET", f"documents/{some}/status"),
        ("DELETE", f"documents/{some}"),
        ("POST", f"documents/{some}/reindex"),
        ("GET", f"ingestion/{some}"),
        ("POST", f"ingestion/{some}/retry"),
    ]:
        assert (await client.request(method, f"{API}/{path}")).status_code == 401, path


# ================================================================ delete (tombstone) and conflicts


async def test_delete_tombstones_immediately_and_queues_a_purge(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    headers = await new_user(client)
    data = sample_text_pdf("gone")
    doc_id = (await upload(client, headers, data)).json()["document"]["id"]
    resp = await client.delete(f"{API}/documents/{doc_id}", headers=headers)
    assert resp.status_code == 202
    assert resp.json() == {"document_id": doc_id, "status": "deleting"}
    assert (await client.get(f"{API}/documents/{doc_id}", headers=headers)).status_code == 404
    assert (await client.get(f"{API}/documents", headers=headers)).json()["total"] == 0
    assert (await client.delete(f"{API}/documents/{doc_id}", headers=headers)).status_code == 404
    assert (
        await scalar(infra, "SELECT count(*) FROM outbox_events WHERE kind = 'purge_document'") == 1
    )
    # the row and the object still exist until the purge task runs (so a failed purge can retry)
    assert (await scalar(infra, "SELECT count(*) FROM documents")) == 1
    assert stored_object_count() == 1
    # the same bytes can be uploaded again right away: a fresh document
    again = await upload(client, headers, data)
    assert again.status_code == 202
    assert again.json()["document"]["id"] != doc_id


async def test_retry_and_reindex_are_refused_while_the_document_is_busy(
    client: httpx2.AsyncClient,
) -> None:
    headers = await new_user(client)
    up = (await upload(client, headers, sample_text_pdf("busy"))).json()
    retry = await client.post(f"{API}/ingestion/{up['job_id']}/retry", headers=headers)
    assert retry.status_code == 409
    assert retry.json()["error"]["code"] == "RETRY_NOT_ALLOWED"
    reindex = await client.post(f"{API}/documents/{up['document']['id']}/reindex", headers=headers)
    assert reindex.status_code == 409
    assert reindex.json()["error"]["code"] == "REINDEX_NOT_ALLOWED"


# ================================================================ state machine under concurrency


async def test_concurrent_transitions_have_exactly_one_winner(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    headers = await new_user(client)
    doc_id = uuid.UUID(
        (await upload(client, headers, sample_text_pdf("cas"))).json()["document"]["id"]
    )
    factory = infra.sync_session_factory
    results: list[str] = []
    barrier = threading.Barrier(8)

    def attempt() -> None:
        with factory() as session:
            store = IngestionStore(session)
            barrier.wait()
            try:
                store.transition(doc_id, DocumentStatus.QUEUED, DocumentStatus.PROCESSING)
                results.append("won")
            except LostStateError:
                results.append("lost")

    threads = [threading.Thread(target=attempt) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == ["lost"] * 7 + ["won"]
    assert await scalar(infra, "SELECT status FROM documents") == "processing"


async def test_transition_on_a_deleted_document_is_refused(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    headers = await new_user(client)
    doc_id = (await upload(client, headers, sample_text_pdf("del"))).json()["document"]["id"]
    await client.delete(f"{API}/documents/{doc_id}", headers=headers)
    with infra.sync_session_factory() as session, pytest.raises(LostStateError):
        IngestionStore(session).transition(
            uuid.UUID(doc_id), DocumentStatus.QUEUED, DocumentStatus.PROCESSING
        )


# ================================================================ outbox dispatcher


class StubCelery:
    def __init__(self, fail: bool = False) -> None:
        self.sent: list[tuple[str, list[str]]] = []
        self.fail = fail
        self._lock = threading.Lock()

    def send_task(self, name: str, args: list[str]) -> None:
        if self.fail:
            raise ConnectionError("broker down")
        with self._lock:
            self.sent.append((name, args))


async def seed_events(infra: Infrastructure, n: int) -> None:
    async with infra.session_factory() as session:
        for i in range(n):
            await session.execute(
                text("INSERT INTO outbox_events (kind, payload) VALUES ('ingest_document', :p)"),
                {"p": f'{{"job_id": "{uuid.uuid4()}", "n": {i}}}'},
            )
        await session.commit()


async def test_outbox_row_is_not_lost_when_the_broker_is_down_and_is_sent_later(
    infra: Infrastructure,
) -> None:
    await seed_events(infra, 3)
    factory = infra.sync_session_factory
    down = StubCelery(fail=True)
    assert dispatch_outbox(factory, down, batch_size=50) == 0  # type: ignore[arg-type]
    assert await scalar(infra, "SELECT count(*) FROM outbox_events WHERE sent_at IS NULL") == 3
    assert await scalar(infra, "SELECT min(attempts) FROM outbox_events") == 1
    assert "broker down" in await scalar(infra, "SELECT last_error FROM outbox_events LIMIT 1")
    up = StubCelery()
    assert dispatch_outbox(factory, up, batch_size=50) == 3  # type: ignore[arg-type]
    assert len(up.sent) == 3
    assert await scalar(infra, "SELECT count(*) FROM outbox_events WHERE sent_at IS NULL") == 0


async def test_concurrent_dispatchers_never_publish_an_event_twice(infra: Infrastructure) -> None:
    await seed_events(infra, 60)
    stub = StubCelery()
    totals: list[int] = []
    barrier = threading.Barrier(4)

    def run() -> None:
        barrier.wait()
        while True:
            sent = dispatch_outbox(infra.sync_session_factory, cast(Celery, stub), batch_size=7)
            totals.append(sent)
            if sent == 0:
                return

    threads = [threading.Thread(target=run) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(totals) == 60
    assert len(stub.sent) == 60
    assert len({tuple(a) for _, a in stub.sent}) == 60  # every job id exactly once


# ================================================================ reconciler


async def age(infra: Infrastructure, table: str, column: str, seconds: int) -> None:
    async with infra.session_factory() as session:
        await session.execute(
            text(f"UPDATE {table} SET {column} = now() - make_interval(secs => :s)"),  # noqa: S608
            {"s": seconds},
        )
        await session.commit()


async def test_reconciler_republishes_lost_queued_jobs_once_per_ttl(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    headers = await new_user(client)
    up = (await upload(client, headers, sample_text_pdf("lost"))).json()
    # the original message is "sent" but the broker lost it; the job stays queued
    async with infra.session_factory() as session:
        await session.execute(text("UPDATE outbox_events SET sent_at = now()"))
        await session.commit()
    await age(infra, "ingestion_jobs", "updated_at", 60)
    await age(infra, "outbox_events", "created_at", 60)
    with infra.sync_session_factory() as session:
        first = reconcile(session, get_settings())
        second = reconcile(session, get_settings())  # within the ttl: must not pile up duplicates
    assert first.requeued_queued == 1
    assert second.requeued_queued == 0
    unsent = await scalar(
        infra,
        "SELECT count(*) FROM outbox_events WHERE sent_at IS NULL AND kind = :k",
        k=KIND_INGEST,
    )
    assert unsent == 1
    assert (
        await scalar(infra, "SELECT payload->>'job_id' FROM outbox_events WHERE sent_at IS NULL")
        == up["job_id"]
    )


async def test_reconciler_recovers_a_crashed_worker_and_gives_up_after_max_attempts(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    headers = await new_user(client)
    up = (await upload(client, headers, sample_text_pdf("crash"))).json()
    doc_id = uuid.UUID(up["document"]["id"])
    # emulate a worker that claimed the job, advanced the document, then died
    with infra.sync_session_factory() as session:
        store = IngestionStore(session)
        store.claim_job(uuid.UUID(up["job_id"]))
        store.transition(doc_id, DocumentStatus.QUEUED, DocumentStatus.PROCESSING)
        store.transition(doc_id, DocumentStatus.PROCESSING, DocumentStatus.EXTRACTING)
    await age(infra, "ingestion_jobs", "updated_at", 60)
    with infra.sync_session_factory() as session:
        report = reconcile(session, get_settings())
    assert report.recovered_processing == 1
    assert await scalar(infra, "SELECT status FROM ingestion_jobs") == "queued"
    assert await scalar(infra, "SELECT status FROM documents") == "queued"
    assert await scalar(infra, "SELECT count(*) FROM outbox_events WHERE sent_at IS NULL") >= 1

    # the same job keeps crashing: after max attempts it is failed (recoverable), not looped forever
    max_attempts = get_settings().ingestion_max_attempts
    with infra.sync_session_factory() as session:
        store = IngestionStore(session)
        for _ in range(max_attempts):
            store.claim_job(uuid.UUID(up["job_id"]))  # attempts += 1, as a worker would
            session.execute(
                text(
                    "UPDATE ingestion_jobs SET status = 'processing', "
                    "updated_at = now() - interval '1 minute'"
                )
            )
            session.commit()
            reconcile(session, get_settings())
    doc = (await client.get(f"{API}/documents/{doc_id}", headers=headers)).json()
    assert doc["status"] == "failed"
    assert doc["error"]["recoverable"] is True
    assert doc["error"]["code"] == "INGESTION_FAILED"


async def test_reconciler_requeues_unfinished_purges(
    client: httpx2.AsyncClient, infra: Infrastructure
) -> None:
    headers = await new_user(client)
    doc_id = (await upload(client, headers, sample_text_pdf("purge"))).json()["document"]["id"]
    await client.delete(f"{API}/documents/{doc_id}", headers=headers)
    await age(infra, "documents", "deleted_at", 60)
    await age(infra, "outbox_events", "created_at", 60)
    with infra.sync_session_factory() as session:
        report = reconcile(session, get_settings())
    assert report.purges_requeued == 1
    assert (
        await scalar(
            infra,
            "SELECT count(*) FROM outbox_events WHERE kind = 'purge_document' AND sent_at IS NULL",
        )
        >= 1
    )


def test_beat_schedule_registers_dispatcher_and_reconciler() -> None:
    from app.tasks.celery_app import celery_app

    schedule = celery_app.conf.beat_schedule
    assert {e["task"] for e in schedule.values()} == {
        "ingestion.dispatch_outbox",
        "ingestion.reconcile",
    }
    assert timedelta(seconds=schedule["dispatch-outbox"]["schedule"]) <= timedelta(seconds=30)
    assert utcnow() is not None
