"""Frontend settings. The UI knows only the API's base URL; it has no database or model access."""

from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_API_URL = "http://127.0.0.1:8000/api/v1"


@dataclass(frozen=True)
class FrontendConfig:
    api_base_url: str = DEFAULT_API_URL
    timeout_s: float = 30.0
    upload_timeout_s: float = 120.0
    # a streamed answer can legitimately take a while (retrieval + model + repair)
    stream_timeout_s: float = 180.0
    poll_interval_s: int = 2
    # Shared with the API's FRONTEND_CLIENT_IP_SECRET; lets per-IP limits see the end user instead
    # of this server. Empty = do not forward the address.
    client_ip_secret: str = ""
    # Free-tier variant: sign-up asks for the deployment's invite code.
    invite_required: bool = False
    # Free hosting puts the API to sleep when idle; waking it takes about a minute. When > 0, the
    # first page load waits up to this long for the API (with a message) instead of failing.
    wake_timeout_s: float = 0.0

    @classmethod
    def from_env(cls) -> FrontendConfig:
        return cls(
            api_base_url=os.environ.get("FRONTEND_API_URL", DEFAULT_API_URL).rstrip("/"),
            client_ip_secret=os.environ.get("FRONTEND_CLIENT_IP_SECRET", ""),
            invite_required=os.environ.get("FRONTEND_INVITE_REQUIRED", "").lower()
            in {"1", "true", "yes"},
            wake_timeout_s=float(os.environ.get("FRONTEND_WAKE_TIMEOUT_S", "0") or 0),
        )
