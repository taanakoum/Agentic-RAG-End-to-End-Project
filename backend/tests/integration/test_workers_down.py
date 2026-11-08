from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import create_app

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    with TestClient(create_app(get_settings())) as c:
        yield c


def test_workers_endpoint_503_when_no_worker_running(client: TestClient) -> None:
    # No worker fixture in this module and the previous module's worker has been shut down.
    r = client.get("/api/v1/health/workers")
    assert r.status_code == 503
    assert r.json() == {"status": "error", "components": {"celery_workers": {"status": "error"}}}
