"""Subprocess for the orphan test: writes the object, then dies before any database commit.

Run as: python backend/tests/crash_upload.py <storage_key>   (cwd=backend, PYTHONPATH=backend)
This is exactly the failure window of an upload: object stored, document row never committed.
"""

from __future__ import annotations

import io
import os
import sys

from app.core.config import get_settings
from app.storage.factory import create_storage


def main() -> None:
    storage = create_storage(get_settings())
    storage.put(sys.argv[1], io.BytesIO(b"%PDF-1.4 orphan"))
    os._exit(1)  # hard crash: no finally blocks, no cleanup, no DB commit


if __name__ == "__main__":
    main()
