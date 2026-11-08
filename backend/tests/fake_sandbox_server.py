"""A stand-in child that speaks the warm-sandbox JSON-line protocol, with misbehaving commands."""

from __future__ import annotations

import json
import os
import sys
import time


def main() -> None:
    out = sys.stdout
    sys.stdout = sys.stderr
    out.write(json.dumps({"ready": True}) + "\n")
    out.flush()
    for line in sys.stdin:
        request = json.loads(line)
        command = request["command"]
        if command == "echo":
            result = {"ok": True, "pid": os.getpid(), "value": request["opts"].get("value")}
        elif command == "sleep":
            time.sleep(float(request["opts"]["seconds"]))
            result = {"ok": True, "pid": os.getpid(), "t": time.time()}
        elif command == "die":
            os._exit(7)
        elif command == "alloc":
            blob = bytearray(int(request["opts"]["mb"]) * 1024 * 1024)
            result = {"ok": True, "size": len(blob)}
        elif command == "env":
            result = {"ok": True, "jwt": os.environ.get("JWT_SECRET"), "cwd": os.getcwd()}
        elif command == "raise":
            result = {"ok": False, "crashed": True, "message": "RuntimeError: simulated"}
        elif command == "garbage":
            out.write("this is not json\n")
            out.flush()
            continue
        else:
            result = {"ok": False, "message": "unknown"}
        out.write(json.dumps(result) + "\n")
        out.flush()


if __name__ == "__main__":
    main()
