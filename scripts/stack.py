"""Start, stop and inspect the whole local application (API, worker, beat, Streamlit UI).

    python scripts/stack.py start      # migrate, start API + worker + beat + UI, wait until healthy
    python scripts/stack.py status
    python scripts/stack.py logs api   # tail a log (api | worker | beat | ui)
    python scripts/stack.py stop

Cross-platform (Windows, macOS, Linux). It starts only the application processes; PostgreSQL, Redis
and Qdrant must already be running (Windows native: scripts/dev_services.ps1 start; Docker:
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d postgres redis qdrant).
Run it with the project's Python (`uv run --no-sync python scripts/stack.py start`).
Logs go to data/logs, process ids to data/run (both gitignored).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOGS = ROOT / "data" / "logs"
RUN = ROOT / "data" / "run"
PIDS = RUN / "pids.json"
WINDOWS = os.name == "nt"


def port_open(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket() as probe:
        probe.settimeout(0.5)
        return probe.connect_ex((host, port)) == 0


def http_ok(url: str, timeout: float = 3.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310  (local URL)
            return int(response.status) == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def load_pids() -> dict[str, int]:
    try:
        return {str(k): int(v) for k, v in json.loads(PIDS.read_text(encoding="utf-8")).items()}
    except (OSError, ValueError):
        return {}


def alive(pid: int) -> bool:
    if WINDOWS:
        out = subprocess.run(  # noqa: S603  (fixed arguments)
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],  # noqa: S607  (Windows system tool)
            capture_output=True,
            text=True,
            check=False,
        ).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def kill_tree(pid: int) -> None:
    if WINDOWS:
        subprocess.run(  # noqa: S603  (fixed arguments)
            ["taskkill", "/PID", str(pid), "/T", "/F"],  # noqa: S607  (Windows system tool)
            capture_output=True,
            check=False,
        )
    else:
        with contextlib.suppress(OSError):
            os.killpg(pid, signal.SIGTERM)  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType]


def spawn(name: str, args: list[str], env: dict[str, str]) -> int:
    LOGS.mkdir(parents=True, exist_ok=True)
    log = (LOGS / f"{name}.log").open("ab")
    common: dict[str, object] = {
        "cwd": ROOT,
        "env": env,
        "stdout": log,
        "stderr": subprocess.STDOUT,
        "stdin": subprocess.DEVNULL,
    }
    if not WINDOWS:
        return subprocess.Popen(args, start_new_session=True, **common).pid  # type: ignore[call-overload]  # noqa: S603
    # Windows: a hidden console of its own, in its own process group. (DETACHED_PROCESS looked
    # right but the children then died with STATUS_CONTROL_C_EXIT as soon as the launcher's console
    # went away; this combination keeps them running after `stack.py start` returns.)
    flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    return subprocess.Popen(args, creationflags=flags, **common).pid  # type: ignore[call-overload]  # noqa: S603


def wait_for(what: str, check: object, seconds: int) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if callable(check) and check():
            print(f"  ok: {what}")
            return True
        time.sleep(1)
    print(f"  TIMEOUT: {what} (see data/logs)")
    return False


def start(args: argparse.Namespace) -> int:
    if not (ROOT / ".env").exists():
        print("No .env yet: run  python scripts/init_env.py  first.")
        return 1
    missing = [
        f"{n} (port {p})"
        for n, p in (("PostgreSQL", 5432), ("Redis", 6379), ("Qdrant", 6333))
        if not port_open(p)
    ]
    if missing:
        print("These services are not reachable: " + ", ".join(missing))
        print("Windows native: powershell -File scripts/dev_services.ps1 start")
        print("Docker:         docker compose -f docker-compose.yml -f docker-compose.dev.yml \\")
        print("                  up -d postgres redis qdrant")
        return 1
    pids = load_pids()
    if any(alive(pid) for pid in pids.values()):
        print("The stack seems to be running already (python scripts/stack.py status | stop).")
        return 1

    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(ROOT / "backend"), str(ROOT)])}
    env.setdefault("FRONTEND_API_URL", f"http://127.0.0.1:{args.api_port}/api/v1")
    # load the embedding + re-ranking models at API start-up, not on the first question
    env.setdefault("WARM_MODELS_ON_STARTUP", "true")
    python = sys.executable

    if not args.no_migrate:
        print("Applying database migrations ...")
        done = subprocess.run(  # noqa: S603  (our own command)
            [
                python,
                "-m",
                "alembic",
                "-c",
                str(ROOT / "backend" / "alembic.ini"),
                "upgrade",
                "head",
            ],
            cwd=ROOT,
            env=env,
            check=False,
        )
        if done.returncode != 0:
            print("Migration failed: is the database created and the .env password right?")
            print("See docs/LOCAL_SETUP.md, section Troubleshooting.")
            return done.returncode

    processes = {
        "api": [
            python,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(args.api_port),
            # psycopg async (LangGraph checkpoints) cannot use the Proactor loop uvicorn picks on
            # Windows; see app.chat.checkpointer.selector_event_loop
            *(["--loop", "app.chat.checkpointer:selector_event_loop"] if WINDOWS else []),
        ],
        "worker": [
            python,
            "-m",
            "celery",
            "-A",
            "app.tasks.celery_app:celery_app",
            "worker",
            "--loglevel=INFO",
            "--pool=solo" if WINDOWS else "--concurrency=2",
        ],
        "beat": [
            python,
            "-m",
            "celery",
            "-A",
            "app.tasks.celery_app:celery_app",
            "beat",
            "--loglevel=INFO",
        ],
        "ui": [
            python,
            "-m",
            "streamlit",
            "run",
            str(ROOT / "frontend" / "app.py"),
            "--server.port",
            str(args.ui_port),
            "--server.headless",
            "true",
            "--browser.gatherUsageStats",
            "false",
        ],
    }
    RUN.mkdir(parents=True, exist_ok=True)
    started: dict[str, int] = {}
    for name, command in processes.items():
        started[name] = spawn(name, command, env)
        print(f"started {name} (pid {started[name]})")
    PIDS.write_text(json.dumps(started), encoding="utf-8")

    print("Waiting for the services (the first start loads the search models: ~1 minute) ...")
    ok = wait_for(
        "API ready", lambda: http_ok(f"http://127.0.0.1:{args.api_port}/api/v1/health/ready"), 180
    )
    ok &= wait_for("UI up", lambda: http_ok(f"http://127.0.0.1:{args.ui_port}/_stcore/health"), 60)
    ok &= wait_for(
        "worker answers",
        lambda: http_ok(f"http://127.0.0.1:{args.api_port}/api/v1/health/workers", timeout=10),
        90,
    )
    print()
    print(f"UI:      http://127.0.0.1:{args.ui_port}")
    print(f"API:     http://127.0.0.1:{args.api_port}/docs   (interactive API docs, dev only)")
    print("Logs:    python scripts/stack.py logs api|worker|beat|ui")
    print("Stop:    python scripts/stack.py stop")
    return 0 if ok else 2


def stop(_: argparse.Namespace) -> int:
    pids = load_pids()
    if not pids:
        print("Nothing recorded as running.")
        return 0
    for name, pid in pids.items():
        if alive(pid):
            kill_tree(pid)
            print(f"stopped {name} (pid {pid})")
    PIDS.unlink(missing_ok=True)
    return 0


def status(args: argparse.Namespace) -> int:
    pids = load_pids()
    for name in ("api", "worker", "beat", "ui"):
        pid = pids.get(name)
        state = f"running (pid {pid})" if pid and alive(pid) else "not running"
        print(f"{name:7} {state}")
    for name, port in (("PostgreSQL", 5432), ("Redis", 6379), ("Qdrant", 6333)):
        print(f"{name:11} {'up' if port_open(port) else 'DOWN'}")
    ready = http_ok(f"http://127.0.0.1:{args.api_port}/api/v1/health/ready")
    print(f"API ready   {'yes' if ready else 'no'}")
    return 0


def logs(args: argparse.Namespace) -> int:
    path = LOGS / f"{args.name}.log"
    if not path.exists():
        print(f"No log yet: {path}")
        return 1
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-args.lines :]
    print("\n".join(lines))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--api-port", type=int, default=8000)
    parser.add_argument("--ui-port", type=int, default=8501)
    sub = parser.add_subparsers(dest="command", required=True)
    start_parser = sub.add_parser("start")
    start_parser.add_argument("--no-migrate", action="store_true")
    sub.add_parser("stop")
    sub.add_parser("status")
    logs_parser = sub.add_parser("logs")
    logs_parser.add_argument("name", choices=["api", "worker", "beat", "ui"])
    logs_parser.add_argument("-n", "--lines", type=int, default=60)
    args = parser.parse_args()
    return {"start": start, "stop": stop, "status": status, "logs": logs}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
