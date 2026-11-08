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

import atexit
import contextlib
import json
import os
import queue
import signal
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable
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
        if limits.memory_mb:
            cap = limits.memory_mb * 1024 * 1024
            # RLIMIT_DATA counts writable heap + anonymous mappings (close to real memory use).
            # RLIMIT_AS also counts reserved address space, which breaks PyTorch/ONNX runtimes.
            kind = resource.RLIMIT_AS if limits.address_space else resource.RLIMIT_DATA
            resource.setrlimit(kind, (cap, cap))
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
        decoded: Any = json.loads(stdout.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise SandboxError("protocol", "child did not return valid JSON") from exc
    if not isinstance(decoded, dict):
        raise SandboxError("protocol", "child returned a non-object")
    return cast(dict[str, Any], decoded)


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


# ====================================================================== warm (persistent) children


class PersistentSandbox:
    """One long-lived sandboxed child speaking JSON lines. Same limits as `run_sandboxed`.

    Why: starting a Python process and importing pypdf costs ~0.4 s, and loading Docling's models
    ~15 s. A warm child pays that once and then answers requests in milliseconds. The child is
    killed and replaced when a call times out, when it crashes, or after `max_jobs` calls (which
    bounds any memory growth or state leakage between documents). Calls are serialised per child.
    """

    def __init__(
        self,
        argv: list[str],
        *,
        limits: SandboxLimits,
        extra_env: dict[str, str] | None = None,
        max_jobs: int = 25,
        startup_timeout_s: float = 180.0,
    ) -> None:
        self._argv = argv
        self._limits = limits
        self._extra_env = extra_env or {}
        self._max_jobs = max_jobs
        self._startup_timeout_s = startup_timeout_s
        self._lock = threading.Lock()
        self._process: subprocess.Popen[bytes] | None = None
        self._job: Any = None
        self._lines: queue.Queue[bytes | None] = queue.Queue()
        self._workdir: tempfile.TemporaryDirectory[str] | None = None
        self._stderr: Any = None
        self._jobs_done = 0
        self.spawn_count = 0  # observable for tests and metrics

    # -- lifecycle ----------------------------------------------------------------------------

    def _spawn(self) -> None:
        self._workdir = tempfile.TemporaryDirectory(prefix="sandbox-warm-")
        workdir = self._workdir.name
        env = scrubbed_env({"TEMP": workdir, "TMP": workdir, "TMPDIR": workdir, **self._extra_env})
        kwargs: dict[str, Any] = {}
        if sys.platform == "win32":
            self._job = _make_job(self._limits.memory_mb)
            kwargs["creationflags"] = 0x00000200
        else:  # pragma: no cover  (exercised by sandbox_selfcheck inside the Linux container)
            kwargs["preexec_fn"] = _posix_session_with_memory(self._limits)
        self._stderr = tempfile.TemporaryFile()  # noqa: SIM115  (closed in _kill)
        self._process = cast(
            "subprocess.Popen[bytes]",
            subprocess.Popen(  # noqa: S603  (argv is built by our own code)
                self._argv,
                cwd=workdir,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=self._stderr,
                **kwargs,
            ),
        )
        if self._job is not None:
            _assign(self._job, self._process)
        self._lines = queue.Queue()
        threading.Thread(
            target=self._read_loop, args=(self._process, self._lines), daemon=True
        ).start()
        self.spawn_count += 1
        self._jobs_done = 0
        try:
            line = self._lines.get(timeout=self._startup_timeout_s)
        except queue.Empty as exc:
            self._kill()
            raise SandboxError("timeout", "sandbox child did not start in time") from exc
        if line is None or json.loads(line).get("ready") is not True:
            tail = self._stderr_tail()
            self._kill()
            raise SandboxError("crash", f"sandbox child failed to start: {tail}")

    @staticmethod
    def _read_loop(process: subprocess.Popen[bytes], lines: queue.Queue[bytes | None]) -> None:
        stdout = process.stdout
        if stdout is not None:
            for line in iter(stdout.readline, b""):
                lines.put(line)
        lines.put(None)  # EOF: the child exited

    def _stderr_tail(self) -> str:
        if self._stderr is None:
            return ""
        try:
            self._stderr.flush()
            self._stderr.seek(0, os.SEEK_END)
            size = self._stderr.tell()
            self._stderr.seek(max(0, size - 600))
            return self._stderr.read().decode("utf-8", "replace").strip()
        except (OSError, ValueError):
            return ""

    def _kill(self) -> None:
        process, self._process = self._process, None
        if process is not None:
            _kill_tree(process)
            for stream in (process.stdin, process.stdout):
                if stream is not None:
                    with contextlib.suppress(OSError):
                        stream.close()
            process.wait()
        if self._job is not None:
            _close_job(self._job)
            self._job = None
        if self._stderr is not None:
            self._stderr.close()
            self._stderr = None
        if self._workdir is not None:
            self._workdir.cleanup()
            self._workdir = None

    def close(self) -> None:
        with self._lock:
            self._kill()

    # -- calls ---------------------------------------------------------------------------------

    def call(self, request: dict[str, Any], *, timeout_s: float) -> dict[str, Any]:
        with self._lock:
            if (
                self._process is None
                or self._process.poll() is not None
                or self._jobs_done >= self._max_jobs
            ):
                self._kill()
                self._spawn()
            process = self._process
            if process is None or process.stdin is None:
                raise SandboxError("crash", "sandbox child is not running")
            try:
                process.stdin.write(json.dumps(request).encode("utf-8") + b"\n")
                process.stdin.flush()
            except OSError as exc:
                tail = self._stderr_tail()
                self._kill()
                raise SandboxError("crash", f"sandbox child is gone: {tail}") from exc
            try:
                line = self._lines.get(timeout=timeout_s)
            except queue.Empty as exc:
                self._kill()  # a hung child is never reused
                raise SandboxError("timeout", f"exceeded {timeout_s:.0f}s") from exc
            if line is None:
                tail = self._stderr_tail()
                self._kill()
                kind = "memory" if "MemoryError" in tail or "Cannot allocate" in tail else "crash"
                raise SandboxError(kind, f"sandbox child exited: {tail[-300:]}")
            if len(line) > MAX_OUTPUT_BYTES:
                self._kill()
                raise SandboxError("output", "child produced too much output")
            self._jobs_done += 1
        try:
            decoded: Any = json.loads(line)
        except ValueError as exc:
            raise SandboxError("protocol", "child did not return valid JSON") from exc
        if not isinstance(decoded, dict):
            raise SandboxError("protocol", "child returned a non-object")
        payload = cast(dict[str, Any], decoded)
        if payload.get("crashed"):
            raise SandboxError("crash", str(payload.get("message", "child raised an exception")))
        return payload


def _posix_session_with_memory(limits: SandboxLimits) -> Any:  # pragma: no cover
    def apply() -> None:
        if sys.platform == "win32":
            return
        import resource

        os.setsid()
        if limits.memory_mb:
            cap = limits.memory_mb * 1024 * 1024
            kind = resource.RLIMIT_AS if limits.address_space else resource.RLIMIT_DATA
            resource.setrlimit(kind, (cap, cap))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

    return apply


class SandboxPool:
    """A fixed number of warm children; callers wait (without spinning) when all are busy."""

    def __init__(self, factory: Callable[[], PersistentSandbox], size: int) -> None:
        self._all = [factory() for _ in range(size)]
        self._idle: queue.LifoQueue[PersistentSandbox] = queue.LifoQueue()
        for sandbox in self._all:
            self._idle.put(sandbox)
        self.size = size
        atexit.register(self.close)

    def call(self, request: dict[str, Any], *, timeout_s: float) -> dict[str, Any]:
        sandbox = self._idle.get()
        try:
            return sandbox.call(request, timeout_s=timeout_s)
        finally:
            self._idle.put(sandbox)

    @property
    def spawn_count(self) -> int:
        return sum(s.spawn_count for s in self._all)

    def close(self) -> None:
        for sandbox in self._all:
            sandbox.close()
