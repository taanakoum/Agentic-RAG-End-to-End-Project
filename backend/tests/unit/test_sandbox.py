"""The sandbox runner against hostile child processes, and parsers against hostile PDFs."""

from __future__ import annotations

import os
import sys
import time
import zlib
from pathlib import Path
from typing import Any

import pytest

from app.core.config import Settings
from app.ingestion.parsing import ParseError, SandboxedPdfParser, parse_bytes
from app.ingestion.sandbox import (
    SandboxError,
    SandboxLimits,
    child_python,
    run_sandboxed,
    scrubbed_env,
)
from tests.pdfs import sample_text_pdf

PY, ENV = child_python()


def run(code: str, *, timeout_s: float = 30, memory_mb: int | None = None) -> dict[str, Any]:
    return run_sandboxed(
        [PY, "-c", code],
        limits=SandboxLimits(timeout_s=timeout_s, memory_mb=memory_mb),
        extra_env=ENV,
    )


def test_child_returns_json() -> None:
    assert run("import json; print(json.dumps({'ok': 1}))") == {"ok": 1}


def test_runaway_child_is_killed_at_the_timeout() -> None:
    started = time.monotonic()
    with pytest.raises(SandboxError) as e:
        run("import time; time.sleep(120)", timeout_s=2)
    assert e.value.kind == "timeout"
    assert time.monotonic() - started < 15  # killed promptly, not after the sleep


def test_child_that_burns_cpu_in_a_busy_loop_is_killed() -> None:
    with pytest.raises(SandboxError) as e:
        run("while True: pass", timeout_s=2)
    assert e.value.kind == "timeout"


@pytest.mark.skipif(sys.platform != "win32", reason="memory cap uses a Windows Job Object here")
def test_memory_hog_is_stopped_by_the_memory_cap() -> None:
    with pytest.raises(SandboxError) as e:
        run("x = bytearray(3 * 1024**3)", memory_mb=512)
    assert e.value.kind == "memory"


@pytest.mark.skipif(sys.platform != "win32", reason="Job Object process limit is Windows-specific")
def test_child_cannot_spawn_further_processes() -> None:
    with pytest.raises(SandboxError):
        run(
            "import subprocess, sys, json\n"
            "r = subprocess.run([sys.executable, '-c', 'print(1)'], capture_output=True)\n"
            "print(json.dumps({'spawned': r.returncode == 0}))"
        )


def test_secrets_are_not_visible_to_the_child(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "JWT_SECRET",
        "POSTGRES_PASSWORD",
        "MFA_ENCRYPTION_KEY",
        "S3_SECRET_KEY",
        "SMTP_PASSWORD",
    ):
        monkeypatch.setenv(name, "must-not-leak")
    out = run(
        "import os, json\n"
        "bad = [k for k in os.environ if k.upper() in "
        "('JWT_SECRET','POSTGRES_PASSWORD','MFA_ENCRYPTION_KEY','S3_SECRET_KEY','SMTP_PASSWORD')]\n"
        "print(json.dumps({'leaked': bad, 'path': bool(os.environ.get('PATH'))}))"
    )
    assert out == {"leaked": [], "path": True}
    assert "JWT_SECRET" not in scrubbed_env()


def test_child_runs_in_its_own_empty_working_directory() -> None:
    out = run("import os, json; print(json.dumps({'cwd': os.getcwd(), 'files': os.listdir('.')}))")
    assert out["files"] == []
    assert Path(out["cwd"]) != Path.cwd()
    assert not Path(out["cwd"]).exists()  # removed after the run


@pytest.mark.parametrize(
    ("code", "kind"),
    [
        ("print('not json')", "protocol"),
        ("print('[1, 2]')", "protocol"),
        ("import sys; sys.exit(3)", "crash"),
        ("import os; os._exit(9)", "crash"),
        ("raise RuntimeError('boom')", "crash"),
    ],
)
def test_bad_children_become_classified_errors(code: str, kind: str) -> None:
    with pytest.raises(SandboxError) as e:
        run(code)
    assert e.value.kind == kind


def test_parent_survives_a_crashing_child_and_keeps_working() -> None:
    with pytest.raises(SandboxError):
        run("import os; os._exit(1)")
    assert run("import json; print(json.dumps({'still': 'alive'}))") == {"still": "alive"}


# ============================================================ hostile PDFs through the real parser


def settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "app_env": "dev",
        "jwt_secret": "x" * 40,
        "mfa_encryption_key": "dGVzdC1vbmx5LWZlcm5ldC1rZXktMzItYnl0ZXMhISE=",
        "pdf_pipeline": "light",
        "parse_memory_mb": 1024,
        "parse_timeout_base_s": 60,
    }
    return Settings.build(**{**base, **overrides})


def pdf_with_stream(stream_dict: str, data: bytes) -> bytes:
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R >>",
        f"<< {stream_dict} /Length {len(data)} >>\nstream\n".encode() + data + b"\nendstream",
    ]
    out = b"%PDF-1.4\n"
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return out


def test_decompression_bomb_does_not_hurt_the_parent() -> None:
    """~1.5 GB of zeros compressed to ~1.5 MB: classic PDF decompression bomb."""
    compressor = zlib.compressobj(9)
    block = b"BT /F1 12 Tf (x) Tj ET\n" + b"\x00" * (1024 * 1024)
    parts = [compressor.compress(block) for _ in range(1500)]
    parts.append(compressor.flush())
    bomb = pdf_with_stream("/Filter /FlateDecode", b"".join(parts))
    assert len(bomb) < 5 * 1024 * 1024
    parser = SandboxedPdfParser(settings())
    started = time.monotonic()
    try:
        result = parse_bytes(parser, bomb, title="bomb", page_count=1)
        assert result.chunks == []  # nothing meaningful came out of it
    except ParseError as exc:
        assert exc.code in {
            "DOCUMENT_PARSE_MEMORY",
            "DOCUMENT_PARSE_TIMEOUT",
            "DOCUMENT_PARSE_FAILED",
            "DOCUMENT_CORRUPTED",
        }
    assert time.monotonic() - started < 90
    # the test process itself is fine and the parser still works afterwards
    assert parse_bytes(parser, sample_text_pdf("after-bomb"), title="ok", page_count=2).chunks


def test_self_referencing_page_tree_is_a_clean_error() -> None:
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [2 0 R] /Count 1 >>",  # the page tree contains itself
    ]
    out = b"%PDF-1.4\n"
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 3\n0000000000 65535 f \n"
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size 3 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    parser = SandboxedPdfParser(settings())
    with pytest.raises(ParseError):
        parse_bytes(parser, out, title="loop", page_count=1)


def test_parse_timeout_is_enforced_and_classified() -> None:
    parser = SandboxedPdfParser(settings(parse_timeout_base_s=0.05, parse_timeout_per_page_s=0.01))
    with pytest.raises(ParseError) as e:
        parse_bytes(parser, sample_text_pdf("slow"), title="t", page_count=1)
    assert e.value.code == "DOCUMENT_PARSE_TIMEOUT"


def test_docling_crash_falls_back_to_the_light_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    parser = SandboxedPdfParser(settings(pdf_pipeline="docling"))

    def boom(self: SandboxedPdfParser, path: Path, title: str, page_count: int) -> Any:
        raise SandboxError("crash", "docling exploded")

    monkeypatch.setattr(SandboxedPdfParser, "_parse_docling", boom)
    result = parse_bytes(parser, sample_text_pdf("fallback"), title="t", page_count=2)
    assert result.parser == "light"
    assert result.chunks


@pytest.mark.parametrize("kind", ["timeout", "memory"])
def test_docling_timeout_or_memory_do_not_fall_back(
    monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    parser = SandboxedPdfParser(settings(pdf_pipeline="docling"))

    def killed(self: SandboxedPdfParser, path: Path, title: str, page_count: int) -> Any:
        raise SandboxError(kind, "killed")

    monkeypatch.setattr(SandboxedPdfParser, "_parse_docling", killed)
    with pytest.raises(ParseError) as e:
        parse_bytes(parser, sample_text_pdf("nofallback"), title="t", page_count=2)
    assert e.value.code == f"DOCUMENT_PARSE_{kind.upper()}"


def test_sandbox_env_does_not_inherit_the_test_process_secrets() -> None:
    os.environ["SMTP_PASSWORD"] = "x"
    try:
        assert "SMTP_PASSWORD" not in scrubbed_env()
    finally:
        del os.environ["SMTP_PASSWORD"]
