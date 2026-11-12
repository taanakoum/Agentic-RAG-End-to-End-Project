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
    poll_interval_s: int = 3

    @classmethod
    def from_env(cls) -> FrontendConfig:
        return cls(api_base_url=os.environ.get("FRONTEND_API_URL", DEFAULT_API_URL).rstrip("/"))
