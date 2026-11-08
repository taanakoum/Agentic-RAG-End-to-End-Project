from __future__ import annotations

from collections.abc import Iterator

import pytest
from celery.contrib.testing.worker import start_worker
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import create_app
from app.tasks.celery_app import celery_app
from app.tasks.system import echo

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def worker() -> Iterator[None]:
    """A real Celery worker (thread, solo pool) against the real Redis broker (db 15)."""
    with start_worker(celery_app, pool="solo", perform_ping_check=False, shutdown_timeout=30):
        yield


@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    with TestClient(create_app(get_settings())) as c:
        yield c


def test_task_roundtrip_through_redis(worker: None) -> None:
    result = echo.delay("hello-celery")
    assert result.get(timeout=20) == "hello-celery"


def test_live(client: TestClient) -> None:
    r = client.get("/api/v1/health/live")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_ready_ok_with_real_services(client: TestClient) -> None:
    r = client.get("/api/v1/health/ready")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert set(body["components"]) == {"postgres", "redis", "qdrant", "storage"}
    assert all(c["status"] == "ok" for c in body["components"].values())


def test_workers_healthy_when_worker_running(client: TestClient, worker: None) -> None:
    r = client.get("/api/v1/health/workers")
    assert r.status_code == 200, r.text
    assert r.json()["components"]["celery_workers"]["status"] == "ok"


def test_request_id_generated_and_echoed(client: TestClient) -> None:
    r = client.get("/api/v1/health/live")
    assert len(r.headers["x-request-id"]) == 32
    r2 = client.get("/api/v1/health/live", headers={"X-Request-ID": "caller-supplied-id-1"})
    assert r2.headers["x-request-id"] == "caller-supplied-id-1"
    r3 = client.get("/api/v1/health/live", headers={"X-Request-ID": "bad id with spaces!"})
    assert r3.headers["x-request-id"] != "bad id with spaces!"


def test_ready_reports_503_without_leaking_details_when_qdrant_down() -> None:
    base = get_settings()
    broken = base.model_copy(update={"qdrant_url": "http://127.0.0.1:1"})
    with TestClient(create_app(broken)) as c:
        r = c.get("/api/v1/health/ready")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "error"
    assert body["components"]["qdrant"]["status"] == "error"
    assert body["components"]["postgres"]["status"] == "ok"
    assert "127.0.0.1:1" not in r.text
    assert "Traceback" not in r.text
