"""Real OTLP/HTTP export: a local receiver stands in for Phoenix, Langfuse or a collector."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, ClassVar

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

BACKEND = Path(__file__).resolve().parents[2]


class _Receiver(BaseHTTPRequestHandler):
    received: ClassVar[list[tuple[str, dict[str, str], bytes]]] = []

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        type(self).received.append(
            (self.path, {k.lower(): v for k, v in self.headers.items()}, body)
        )
        self.send_response(200)
        self.send_header("Content-Type", "application/x-protobuf")
        self.end_headers()

    def log_message(self, format: str, *args: Any) -> None:
        return None


def test_spans_reach_an_otlp_http_endpoint_with_auth_header_and_without_user_content() -> None:
    _Receiver.received = []
    server = HTTPServer(("127.0.0.1", 0), _Receiver)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        env = {
            **os.environ,
            "PYTHONPATH": str(BACKEND),
            "OTEL_ENABLED": "true",
            "OTEL_EXPORTER_OTLP_ENDPOINT": f"http://127.0.0.1:{server.server_port}",
            "OTEL_EXPORTER_OTLP_HEADERS": '{"authorization": "Bearer probe-token"}',
            "OTEL_METRICS_ENABLED": "false",
        }
        done = subprocess.run(  # noqa: S603  (our own probe script, fixed arguments)
            [sys.executable, str(Path(__file__).with_name("otlp_probe.py"))],
            cwd=BACKEND,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
    finally:
        server.shutdown()
    assert done.returncode == 0, done.stderr[-2000:]
    traces = [r for r in _Receiver.received if r[0] == "/v1/traces"]
    assert traces, [r[0] for r in _Receiver.received]
    _path, headers, body = traces[0]
    assert headers["authorization"] == "Bearer probe-token"
    request = ExportTraceServiceRequest()
    request.ParseFromString(body)
    names = {s.name for rs in request.resource_spans for ss in rs.scope_spans for s in ss.spans}
    assert {"probe.request", "probe.child"} <= names
    attributes = {a.key for rs in request.resource_spans for a in rs.resource.attributes}
    assert {"service.name", "deployment.environment"} <= attributes
    assert b"must never be exported" not in body  # the denied `query` attribute was filtered
