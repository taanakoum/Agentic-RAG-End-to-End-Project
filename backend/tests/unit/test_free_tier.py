"""Free-tier variant building blocks (ADR-042): hosted embeddings, the in-process task runner,
the free deployment profile and the retrieval fallback. No network, broker or database."""

from __future__ import annotations

import json
import math
import time
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from app.ingestion.embedding import SparseVec
from app.ingestion.hosted_embedding import EmbeddingServiceError, HostedEmbedder
from app.retrieval.models import RetrievalMode
from app.services.retrieval_service import RetrievalService
from app.tasks.celery_app import celery_app
from app.tasks.inline import InlineRunner, _Job  # pyright: ignore[reportPrivateUsage]
from tests.unit.test_config import make

# ------------------------------------------------------------------ hosted embeddings


def embedder(handler: Any, **overrides: Any) -> HostedEmbedder:
    settings = make(
        embedding_backend="hosted",
        embedding_model="gemini-embedding-001",
        embedding_dim=8,
        llm_base_url="https://llm.example.org/v1beta/openai",
        llm_api_key="test-key",
        embedding_api_batch_size=2,
        embedding_api_max_attempts=3,
        **overrides,
    )
    client = httpx.Client(
        base_url="https://llm.example.org/v1beta/openai/", transport=httpx.MockTransport(handler)
    )
    return HostedEmbedder(settings, client=client)


def answer(vectors: list[list[float]]) -> httpx.Response:
    data = [{"index": i, "embedding": v} for i, v in reversed(list(enumerate(vectors)))]
    return httpx.Response(200, json={"data": data})


def test_batches_requests_and_asks_for_the_configured_dimensions() -> None:
    seen: list[dict[str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path.endswith("/openai/embeddings")
        assert req.headers["authorization"] == "Bearer test-key"
        body = json.loads(req.content)
        seen.append(body)
        return answer([[1.0] + [0.0] * 7] * len(body["input"]))

    vectors = embedder(handler).embed_dense(["a", "b", "c"])
    assert len(vectors) == 3
    assert [len(b["input"]) for b in seen] == [2, 1]  # batch size 2
    assert all(b["dimensions"] == 8 and b["model"] == "gemini-embedding-001" for b in seen)


def test_longer_vectors_are_truncated_and_renormalised_in_input_order() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return answer(
            [[3.0, 4.0] + [0.0] * 6 + [9.0, 9.0], [0.0, 0.0, 0.0, 2.0] + [0.0] * 4 + [1.0]]
        )

    first, second = embedder(handler).embed_dense(["x", "y"])
    assert first == pytest.approx([0.6, 0.8] + [0.0] * 6)  # the index order is restored
    assert second == pytest.approx([0.0, 0.0, 0.0, 1.0] + [0.0] * 4)
    assert math.isclose(sum(v * v for v in first), 1.0)


def test_rate_limits_are_retried_and_then_succeed() -> None:
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"retry-after": "0"})
        return answer([[0.0, 1.0] + [0.0] * 6])

    assert embedder(handler).embed_query_dense("q") == pytest.approx([0.0, 1.0] + [0.0] * 6)
    assert calls["n"] == 2


@pytest.mark.parametrize(
    ("response", "why"),
    [
        (httpx.Response(200, json={"data": []}), "wrong number"),
        (httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0, 0.0]}]}), "dimensions"),
        (httpx.Response(401, json={"error": "bad key"}), "401"),
    ],
)
def test_unusable_answers_fail_clearly(response: httpx.Response, why: str) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return response

    with pytest.raises(EmbeddingServiceError, match=why):
        embedder(handler).embed_query_dense("q")


class _LocalBm25:
    def __init__(self, vec: SparseVec) -> None:
        self.vec = vec

    def embed_query_sparse(self, text: str) -> SparseVec:
        return self.vec


def test_sparse_vectors_stay_local() -> None:
    def no_network(req: httpx.Request) -> httpx.Response:
        raise AssertionError("BM25 must not call the embedding service")

    e = embedder(no_network)
    local = SparseVec([1, 2], [0.5, 0.5])
    e._sparse = _LocalBm25(local)  # pyright: ignore[reportPrivateUsage, reportAttributeAccessIssue]
    assert e.embed_query_sparse("q") == local


def test_a_key_is_required() -> None:
    with pytest.raises(EmbeddingServiceError, match="key"):
        HostedEmbedder(make(embedding_backend="hosted", llm_base_url="https://x.example/v1"))


# ------------------------------------------------------------------ in-process runner

_calls: list[tuple[str, int]] = []


@celery_app.task(name="test.inline_ok")
def _ok(value: str) -> None:
    _calls.append(("ok", int(value)))


@celery_app.task(name="test.inline_boom")
def _boom() -> None:
    _calls.append(("boom", 0))
    raise RuntimeError("transient")


@celery_app.task(name="test.inline_retry", bind=True)
def _retry(self: Any) -> None:
    _calls.append(("retry", 0))
    raise self.retry(exc=ValueError("busy"), countdown=5, max_retries=60)


def runner() -> InlineRunner:
    _calls.clear()
    return InlineRunner(make(ingestion_max_attempts=1))  # max_attempts = 1 + 5 = 6


def test_send_task_queues_a_job_that_runs_the_task_function() -> None:
    r = runner()
    r.send_task("test.inline_ok", args=["7"], headers={"traceparent": "x"})
    job = r._heap[0]  # pyright: ignore[reportPrivateUsage]
    assert r.run_job(job) is True
    assert _calls == [("ok", 7)] and r.executed == 1


def test_a_failing_task_is_rescheduled_with_backoff_then_given_up() -> None:
    r = runner()
    job = _Job(time.monotonic(), 0, "test.inline_boom", [])
    assert r.run_job(job) is False
    retry = r._heap[0]  # pyright: ignore[reportPrivateUsage]
    assert retry.attempt == 1 and retry.due - time.monotonic() > 4  # first backoff 5 s
    final = _Job(time.monotonic(), 1, "test.inline_boom", [], attempt=r.max_attempts - 1)
    r._heap.clear()  # pyright: ignore[reportPrivateUsage]
    assert r.run_job(final) is False
    assert r._heap == [] and r.failed == 1  # pyright: ignore[reportPrivateUsage]


def test_celery_retry_raises_instead_of_needing_a_broker() -> None:
    r = runner()
    assert r.run_job(_Job(time.monotonic(), 0, "test.inline_retry", [])) is False
    assert r._heap[0].attempt == 1  # pyright: ignore[reportPrivateUsage]


def test_unknown_tasks_are_dropped_not_retried() -> None:
    r = runner()
    assert r.run_job(_Job(time.monotonic(), 0, "test.does_not_exist", [])) is False
    assert r._heap == []  # pyright: ignore[reportPrivateUsage]


def test_the_periodic_schedule_matches_celery_beat() -> None:
    names = {name for name, _ in runner()._periodic()}  # pyright: ignore[reportPrivateUsage]
    beat = {entry["task"] for entry in celery_app.conf.beat_schedule.values()}
    assert names | {"ingestion.dispatch_outbox"} == beat


# ------------------------------------------------------------------ profile and retrieval

FREE_PROD: dict[str, Any] = {
    "app_env": "prod",
    "deployment_profile": "free",
    "task_runner": "inline",
    "registration_invite_code": "an-invite-code-1",
    "require_verified_email": False,
    "postgres_password": "a-long-unique-secret",
    "log_json": True,
    "argon2_memory_kib": 65536,
    "argon2_time_cost": 3,
}


def test_the_free_profile_runs_without_smtp_when_sign_up_is_invite_only() -> None:
    s = make(**FREE_PROD)
    assert s.email_backend == "file" and not s.require_verified_email


@pytest.mark.parametrize(
    ("override", "why"),
    [
        ({"registration_invite_code": ""}, "registration_invite_code"),
        ({"registration_invite_code": "short"}, "registration_invite_code"),
        ({"task_runner": "celery"}, "task_runner=inline"),
        ({"postgres_sslmode": "disable"}, "verify-full"),
    ],
)
def test_the_free_profile_keeps_its_guards(override: dict[str, Any], why: str) -> None:
    with pytest.raises(ValidationError, match=why):
        make(**{**FREE_PROD, **override})


def test_the_full_profile_still_requires_smtp_and_verification() -> None:
    with pytest.raises(ValidationError, match="require_verified_email"):
        make(**{**FREE_PROD, "deployment_profile": "full"})


@pytest.mark.parametrize(
    ("overrides", "mode"),
    [
        ({}, RetrievalMode.HYBRID_RERANK),
        ({"rerank_enabled": False}, RetrievalMode.HYBRID),
        ({"rerank_enabled": False, "retrieval_mode_without_rerank": "dense"}, RetrievalMode.DENSE),
    ],
)
def test_default_retrieval_mode(overrides: dict[str, Any], mode: RetrievalMode) -> None:
    service = RetrievalService.__new__(RetrievalService)
    service._settings = make(**overrides)  # pyright: ignore[reportPrivateUsage]
    assert service.default_mode() is mode


def test_starting_the_runner_registers_every_scheduled_task() -> None:
    r = runner()
    r._schedule_loop = lambda: None  # pyright: ignore[reportPrivateUsage, reportAttributeAccessIssue]
    r.start()
    try:
        for name, _ in r._periodic():  # pyright: ignore[reportPrivateUsage]
            assert name in celery_app.tasks, name
        assert "ingestion.process_document" in celery_app.tasks
    finally:
        r.stop()


_seen_retries: list[int] = []


@celery_app.task(name="test.inline_counts", bind=True)
def _counts(self: Any) -> None:
    _seen_retries.append(int(self.request.retries or 0))
    raise self.retry(exc=RuntimeError("again"), countdown=1, max_retries=10)


def test_the_task_sees_the_attempt_number_like_under_a_worker() -> None:
    """Without it the ingestion task never reaches its own 'failed after N attempts' path and the
    work cycles forever through the reconciler (found with a real 429-limited embedding quota)."""
    r = runner()
    _seen_retries.clear()
    for attempt in range(3):
        r.run_job(_Job(time.monotonic(), attempt, "test.inline_counts", [], attempt=attempt))
    assert _seen_retries == [0, 1, 2]


def test_a_429_without_retry_after_waits_for_the_quota_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slept: list[float] = []
    monkeypatch.setattr("app.ingestion.hosted_embedding.time.sleep", slept.append)
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429) if calls["n"] == 1 else answer([[1.0] + [0.0] * 7])

    embedder(handler, embedding_api_rate_limit_wait_s=42.0).embed_query_dense("q")
    assert slept == [42.0]


def test_the_inputs_per_minute_window_holds_requests_back(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.ingestion import hosted_embedding as he

    clock = {"t": 1000.0}
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        clock["t"] += seconds

    monkeypatch.setattr(he.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(he.time, "sleep", sleep)
    window = he._InputsPerMinute(3)  # pyright: ignore[reportPrivateUsage]
    window.acquire(2)
    window.acquire(1)
    assert slept == []
    window.acquire(2)  # 3 already used in this minute: wait for the window to roll over
    assert slept and sum(slept) == pytest.approx(60.0)
