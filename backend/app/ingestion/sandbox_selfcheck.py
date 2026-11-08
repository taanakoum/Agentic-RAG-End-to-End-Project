"""Self-check for the sandbox on THIS platform. Run inside the container:

    docker compose run --rm worker python -m app.ingestion.sandbox_selfcheck

Prints one line per check and exits non-zero if any check fails. It exercises the real limits
(timeout kill, memory cap, scrubbed environment, warm child recycling) so that the Linux
behaviour is proven where it will run, not assumed from the Windows tests.
"""

from __future__ import annotations

import os
import secrets
import sys
import tempfile
import time
from pathlib import Path

from app.ingestion.sandbox import (
    PersistentSandbox,
    SandboxError,
    SandboxLimits,
    child_python,
    run_sandboxed,
)

_ECHO_SERVER = (
    "import json, os, sys\n"
    "print(json.dumps({'ready': True}), flush=True)\n"
    "for line in sys.stdin:\n"
    "    print(json.dumps({'pid': os.getpid()}), flush=True)\n"
)


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"{'PASS' if ok else 'FAIL'}  {name} {detail}".rstrip())
    return ok


def main() -> int:
    exe, env = child_python()
    results: list[bool] = []

    def run(code: str, timeout_s: float = 30.0, memory_mb: int | None = None) -> dict[str, object]:
        limits = SandboxLimits(timeout_s=timeout_s, memory_mb=memory_mb)
        return run_sandboxed([exe, "-c", code], limits=limits, extra_env=env)

    os.environ["JWT_SECRET"] = secrets.token_hex(16)  # must never reach the child
    results.append(
        check("child returns JSON", run("import json; print(json.dumps({'ok': 1}))") == {"ok": 1})
    )

    started = time.monotonic()
    try:
        run("import time; time.sleep(120)", timeout_s=2)
        results.append(check("timeout kills the child", False, "no error raised"))
    except SandboxError as exc:
        fast = time.monotonic() - started < 15
        results.append(check("timeout kills the child", exc.kind == "timeout" and fast))

    try:
        run("x = bytearray(3 * 1024**3)", memory_mb=512)
        results.append(check("memory cap stops a 3 GB allocation", False, "allocation succeeded"))
    except SandboxError as exc:
        ok = exc.kind in {"memory", "crash"}
        results.append(check("memory cap stops a 3 GB allocation", ok, exc.kind))

    leaked = run("import os, json; print(json.dumps({'jwt': os.environ.get('JWT_SECRET')}))")
    results.append(check("secrets are not visible to the child", leaked == {"jwt": None}))

    with tempfile.TemporaryDirectory() as directory:
        script = Path(directory) / "echo_server.py"
        script.write_text(_ECHO_SERVER)
        sandbox = PersistentSandbox(
            [exe, str(script)], limits=SandboxLimits(timeout_s=30), extra_env=env, max_jobs=3
        )
        try:
            pids = [sandbox.call({"command": "x"}, timeout_s=10)["pid"] for _ in range(7)]
            results.append(
                check(
                    "warm child is reused and recycled",
                    len(set(pids)) == 3,
                    f"pids={len(set(pids))}",
                )
            )
        finally:
            sandbox.close()

    print("sandbox self-check:", "ALL PASSED" if all(results) else "FAILED", "on", sys.platform)
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
