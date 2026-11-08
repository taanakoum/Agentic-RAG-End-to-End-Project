"""Warm (persistent) sandbox children and the pool in front of them."""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from app.core.config import Settings
from app.ingestion.parsing import SandboxedPdfParser, parse_bytes
from app.ingestion.sandbox import (
    PersistentSandbox,
    SandboxError,
    SandboxLimits,
    SandboxPool,
    child_python,
)
from tests.pdfs import sample_text_pdf

SERVER = Path(__file__).resolve().parents[1] / "fake_sandbox_server.py"
PY, ENV = child_python()


def sandbox(**kw: Any) -> PersistentSandbox:
    limits = SandboxLimits(timeout_s=60, memory_mb=kw.pop("memory_mb", None))
    return PersistentSandbox([PY, str(SERVER)], limits=limits, extra_env=ENV, **kw)


def call(sb: PersistentSandbox, command: str, timeout_s: float = 20, **opts: Any) -> dict[str, Any]:
    return sb.call({"command": command, "path": "-", "opts": opts}, timeout_s=timeout_s)


def test_one_child_serves_many_calls_without_respawning() -> None:
    sb = sandbox()
    try:
        pids = {call(sb, "echo", value=i)["pid"] for i in range(20)}
        assert len(pids) == 1
        assert sb.spawn_count == 1
    finally:
        sb.close()


def test_warm_calls_are_far_faster_than_the_first_call() -> None:
    sb = sandbox()
    try:
        started = time.perf_counter()
        call(sb, "echo")
        first = time.perf_counter() - started
        started = time.perf_counter()
        for _ in range(20):
            call(sb, "echo")
        per_call = (time.perf_counter() - started) / 20
        assert per_call < 0.05  # milliseconds, not the ~0.1-0.4 s of starting a process
        assert per_call < first
    finally:
        sb.close()


def test_child_is_recycled_after_max_jobs() -> None:
    sb = sandbox(max_jobs=3)
    try:
        pids = [call(sb, "echo")["pid"] for _ in range(7)]
        assert len(set(pids)) == 3  # 3 + 3 + 1 calls
        assert sb.spawn_count == 3
    finally:
        sb.close()


def test_a_hung_call_times_out_kills_the_child_and_the_next_call_gets_a_fresh_one() -> None:
    sb = sandbox()
    try:
        first = call(sb, "echo")["pid"]
        started = time.monotonic()
        with pytest.raises(SandboxError) as e:
            call(sb, "sleep", timeout_s=1.5, seconds=60)
        assert e.value.kind == "timeout"
        assert time.monotonic() - started < 15
        second = call(sb, "echo")["pid"]
        assert second != first  # the hung child is never reused
        assert sb.spawn_count == 2
    finally:
        sb.close()


def test_a_crashing_child_is_reported_and_replaced() -> None:
    sb = sandbox()
    try:
        first = call(sb, "echo")["pid"]
        with pytest.raises(SandboxError) as e:
            call(sb, "die")
        assert e.value.kind == "crash"
        assert call(sb, "echo")["pid"] != first
    finally:
        sb.close()


def test_exception_inside_the_child_is_reported_as_a_crash_without_losing_the_process() -> None:
    sb = sandbox()
    try:
        pid = call(sb, "echo")["pid"]
        with pytest.raises(SandboxError) as e:
            call(sb, "raise")
        assert e.value.kind == "crash"
        assert call(sb, "echo")["pid"] == pid  # the loop survived; only that request failed
    finally:
        sb.close()


def test_garbage_output_is_a_protocol_error() -> None:
    sb = sandbox()
    try:
        with pytest.raises(SandboxError) as e:
            call(sb, "garbage")
        assert e.value.kind == "protocol"
    finally:
        sb.close()


@pytest.mark.skipif(sys.platform != "win32", reason="memory cap uses a Windows Job Object here")
def test_memory_cap_applies_to_the_warm_child() -> None:
    sb = sandbox(memory_mb=400)
    try:
        assert call(sb, "alloc", mb=50)["size"] == 50 * 1024 * 1024  # within the cap
        with pytest.raises(SandboxError) as e:
            call(sb, "alloc", mb=2500)
        assert e.value.kind in {"memory", "crash"}
        assert call(sb, "echo")["ok"] is True  # replaced and working again
    finally:
        sb.close()


def test_warm_child_has_no_secrets_and_a_private_working_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JWT_SECRET", "must-not-leak")
    sb = sandbox()
    try:
        out = call(sb, "env")
        assert out["jwt"] is None
        assert Path(out["cwd"]) != Path.cwd()
    finally:
        sb.close()


def test_pool_bounds_concurrency_and_reuses_children() -> None:
    pool = SandboxPool(lambda: sandbox(), size=2)
    stamps: list[tuple[float, float]] = []
    lock = threading.Lock()

    def work() -> None:
        result = pool.call(
            {"command": "sleep", "path": "-", "opts": {"seconds": 0.4}}, timeout_s=20
        )
        with lock:  # the child reports when it FINISHED; it was busy for the 0.4 s before that
            stamps.append((result["t"] - 0.4, result["t"]))

    try:
        threads = [threading.Thread(target=work) for _ in range(6)]
        started = time.monotonic()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        elapsed = time.monotonic() - started
        # 6 jobs of 0.4 s on 2 children take at least 3 rounds, never all at once
        assert elapsed >= 1.1
        overlap = max(sum(1 for s, e in stamps if s <= t <= e) for t, _ in stamps)
        assert overlap <= 2
        assert pool.spawn_count == 2  # two warm children served all six jobs
    finally:
        pool.close()


def test_pool_survives_a_failure_in_one_child() -> None:
    pool = SandboxPool(lambda: sandbox(), size=2)
    try:
        with pytest.raises(SandboxError):
            pool.call({"command": "die", "path": "-", "opts": {}}, timeout_s=20)
        assert pool.call({"command": "echo", "path": "-", "opts": {}}, timeout_s=20)["ok"] is True
    finally:
        pool.close()


# ================================================================ the real parser in pool mode


def settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "app_env": "dev",
        "jwt_secret": "x" * 40,
        "mfa_encryption_key": "dGVzdC1vbmx5LWZlcm5ldC1rZXktMzItYnl0ZXMhISE=",
        "pdf_pipeline": "light",
        "sandbox_mode": "pool",
    }
    return Settings.build(**{**base, **overrides})


def test_parser_pool_reuses_warm_children_across_many_documents() -> None:
    parser = SandboxedPdfParser(settings(sandbox_recycle_jobs=100))
    try:
        started = time.perf_counter()
        for i in range(12):
            result = parse_bytes(parser, sample_text_pdf(f"warm-{i}"), title="t", page_count=2)
            assert result.chunks
        elapsed = time.perf_counter() - started
        assert parser.spawn_count == 1  # one warm "parse" child handled all twelve documents
        assert elapsed < 12 * 0.6  # one-shot would pay ~0.4 s of process start per document
    finally:
        parser.close()


def test_parser_pool_inspect_is_fast_and_concurrent() -> None:
    parser = SandboxedPdfParser(settings(inspect_pool_size=2))
    data = sample_text_pdf("inspect")
    try:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as d:
            path = Path(d) / "a.pdf"
            path.write_bytes(data)
            assert parser.inspect(path).page_count == 2  # warm-up (spawns the children lazily)
            started = time.perf_counter()
            threads = [threading.Thread(target=lambda: parser.inspect(path)) for _ in range(20)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            assert time.perf_counter() - started < 2.0  # 20 inspections, ms each once warm
    finally:
        parser.close()
