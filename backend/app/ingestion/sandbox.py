"""Run untrusted-input parsing in a separate, resource-limited OS process.

Why: PDF parsers (pypdf, Docling, PDFium, ML models) process attacker-controlled bytes. A hostile
or just pathological file can loop forever, exhaust memory or crash the interpreter. In a child
process those become a clean, classified error instead of taking down the API or a Celery worker.

What the child gets:
  * a hard wall-clock timeout (the whole process tree is killed);
  * a memory cap (Windows: Job Object committed-memory limit; POSIX: RLIMIT_AS when requested);
  * no ability to spawn further processes (Windows: ActiveProcessLimit=1; POSIX: new session, and
    the tree is killed on exit);
  * a scrubbed environment (an allow-list: no JWT/DB/Redis/SMTP/S3 secrets);
  * its own empty working/temp directory, stdin closed, output captured with a size cap.
What it does NOT get (needs a container/VM boundary, see docs/SECURITY.md): network or filesystem
isolation. In Docker the worker already runs as an unprivileged user with a read-only code tree.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from app.core.logging import get_logger

log = get_logger(__name__)

MAX_OUTPUT_BYTES = 64 * 1024 * 1024
_ENV_ALLOW = (
    "PATH", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "TMPDIR",
    "LANG", "LC_ALL", "PYTHONPATH", "PYTHONIOENCODING", "HOME", "USERPROFILE", "APPDATA",
    "LOCALAPPDATA", "HF_HOME", "HF_HUB_CACHE", "HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE",
    "XDG_CACHE_HOME", "TORCH_HOME", "DOCLING_ARTIFACTS_PATH", "OMP_NUM_THREADS",
    "MKL_NUM_THREADS", "RAPIDOCR_MODEL_DIR",
)  # fmt: skip


class SandboxError(Exception):
    """The sandboxed process failed. `kind` is one of: timeout, memory, crash, output, protocol."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(f"{kind}: {message}")
        self.kind = kind
        self.message = message


@dataclass(frozen=True, slots=True)
class SandboxLimits:
    timeout_s: float
    memory_mb: int | None = None  # None = no memory cap
    address_space: bool = False  # POSIX only: use RLIMIT_AS (breaks some ML runtimes)


def scrubbed_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k.upper() in _ENV_ALLOW}
    env.update(extra or {})
    return env


# ------------------------------------------------------------------ Windows job object

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
    _JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    _EXTENDED_LIMIT_INFORMATION = 9

    class _BasicLimit(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _IoCounters(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint64) for n in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]  # fmt: skip

    class _ExtendedLimit(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimit),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    def _make_job(memory_mb: int | None) -> Any:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            raise OSError("CreateJobObject failed")
        info = _ExtendedLimit()
        flags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | _JOB_OBJECT_LIMIT_ACTIVE_PROCESS
        info.BasicLimitInformation.ActiveProcessLimit = 1  # the child may not start children
        if memory_mb:
            flags |= _JOB_OBJECT_LIMIT_PROCESS_MEMORY
            info.ProcessMemoryLimit = memory_mb * 1024 * 1024
        info.BasicLimitInformation.LimitFlags = flags
        ok = kernel32.SetInformationJobObject(
            job, _EXTENDED_LIMIT_INFORMATION, ctypes.byref(info), ctypes.sizeof(info)
        )
        if not ok:
            raise OSError("SetInformationJobObject failed")
        return job

    def _assign(job: Any, process: subprocess.Popen[bytes]) -> None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = wintypes.HANDLE(int(process._handle))  # type: ignore[attr-defined]
        if not kernel32.AssignProcessToJobObject(job, handle):
            raise OSError("AssignProcessToJobObject failed")

    def _close_job(job: Any) -> None:
        ctypes.WinDLL("kernel32").CloseHandle(job)  # KILL_ON_JOB_CLOSE ends any survivors


def _posix_preexec(limits: SandboxLimits) -> Any:
    def apply() -> None:  # pragma: no cover  (exercised by the Linux container checks)
        if sys.platform == "win32":
            return
        import resource

        os.setsid()
        if limits.memory_mb and limits.address_space:
            cap = limits.memory_mb * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (cap, cap))
        cpu = int(limits.timeout_s) + 5
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

    return apply


def run_sandboxed(
    argv: list[str],
    *,
    limits: SandboxLimits,
    extra_env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Run `argv` in the sandbox; the child must print exactly one JSON object on stdout."""
    with tempfile.TemporaryDirectory(prefix="sandbox-") as workdir:
        env = scrubbed_env(
            {"TEMP": workdir, "TMP": workdir, "TMPDIR": workdir, **(extra_env or {})}
        )
        kwargs: dict[str, Any] = {}
        job: Any = None
        if sys.platform == "win32":
            job = _make_job(limits.memory_mb)
            kwargs["creationflags"] = 0x00000200  # CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["preexec_fn"] = _posix_preexec(limits)
        process = cast(
            "subprocess.Popen[bytes]",
            subprocess.Popen(  # noqa: S603  (argv is built by our own code, never by users)
                argv,
                cwd=workdir,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                **kwargs,
            ),
        )
        try:
            if job is not None:
                _assign(job, process)
            try:
                stdout, stderr = process.communicate(timeout=limits.timeout_s)
            except subprocess.TimeoutExpired as exc:
                _kill_tree(process)
                process.communicate()
                raise SandboxError("timeout", f"exceeded {limits.timeout_s:.0f}s") from exc
        finally:
            if process.poll() is None:
                _kill_tree(process)
            if job is not None:
                _close_job(job)

    if len(stdout) > MAX_OUTPUT_BYTES:
        raise SandboxError("output", "child produced too much output")
    if process.returncode != 0:
        tail = stderr.decode("utf-8", "replace")[-400:]
        kind = "memory" if "MemoryError" in tail or "Cannot allocate" in tail else "crash"
        log.warning("sandbox_child_failed", returncode=process.returncode, kind=kind)
        raise SandboxError(kind, f"child exited with code {process.returncode}: {tail.strip()}")
    try:
        payload = json.loads(stdout.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise SandboxError("protocol", "child did not return valid JSON") from exc
    if not isinstance(payload, dict):
        raise SandboxError("protocol", "child returned a non-object")
    return payload  # type: ignore[return-value]


def _kill_tree(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    if sys.platform == "win32":
        subprocess.run(  # noqa: S603
            ["taskkill", "/F", "/T", "/PID", str(process.pid)],  # noqa: S607
            capture_output=True,
            check=False,
        )
    else:  # pragma: no cover
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            process.kill()


def child_python() -> tuple[str, dict[str, str]]:
    """The interpreter and PYTHONPATH for a sandboxed child.

    Inside a virtualenv on Windows, `python.exe` is a launcher that starts the real interpreter
    as a second process, which the sandbox's "no child processes" rule forbids. So the child runs
    the base interpreter directly and gets the venv's site-packages (and our code) via PYTHONPATH.
    """
    backend_dir = str(Path(__file__).resolve().parents[2])
    paths = [backend_dir]
    exe = sys.executable
    if sys.prefix != sys.base_prefix:
        exe = getattr(sys, "_base_executable", sys.executable)
        import site

        paths += site.getsitepackages()
    return exe, {"PYTHONPATH": os.pathsep.join(paths)}


def module_command(module: str, *args: str | Path) -> tuple[list[str], dict[str, str]]:
    exe, env = child_python()
    return [exe, "-m", module, *[str(a) for a in args]], env
