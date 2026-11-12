"""Subprocess used by test_otlp_export.py: real setup_telemetry against a local OTLP receiver."""

from __future__ import annotations

from app.core.config import get_settings
from app.core.telemetry import set_attributes, setup_telemetry, shutdown_telemetry, span

if __name__ == "__main__":
    assert setup_telemetry(get_settings(), instrument_libraries=False)
    with span("probe.request", count=3):
        set_attributes(query="must never be exported", ok=True)
        with span("probe.child"):
            pass
    shutdown_telemetry()  # flushes the batch processor
