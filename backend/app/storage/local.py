"""Local-volume object storage adapter (dev and single-node deployments)."""

from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
import tempfile
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

from app.storage.base import (
    InvalidKeyError,
    ObjectInfo,
    ObjectNotFoundError,
    ObjectTooLargeError,
    StorageError,
    StoredObject,
    validate_key,
)

_CHUNK = 1024 * 1024
_TMP_DIR = ".tmp"


class LocalObjectStorage:
    def __init__(self, root: Path) -> None:
        self._root = root.resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        (self._root / _TMP_DIR).mkdir(exist_ok=True)

    def _path(self, key: str) -> Path:
        validate_key(key)
        candidate = (self._root / key).resolve()
        # Defense in depth on top of validate_key: the resolved path must stay inside the root.
        if not candidate.is_relative_to(self._root) or candidate == self._root:
            raise InvalidKeyError("key escapes the storage root")
        return candidate

    def put(self, key: str, stream: BinaryIO, *, max_bytes: int | None = None) -> StoredObject:
        target = self._path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        size = 0
        fd, tmp_name = tempfile.mkstemp(dir=self._root / _TMP_DIR)
        try:
            with os.fdopen(fd, "wb") as out:
                while chunk := stream.read(_CHUNK):
                    size += len(chunk)
                    if max_bytes is not None and size > max_bytes:
                        raise ObjectTooLargeError(f"object exceeds {max_bytes} bytes")
                    digest.update(chunk)
                    out.write(chunk)
                out.flush()
                os.fsync(out.fileno())
            os.replace(tmp_name, target)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp_name)
            raise
        return StoredObject(key=key, size=size, sha256=digest.hexdigest())

    def open(self, key: str) -> BinaryIO:
        path = self._path(key)
        try:
            return path.open("rb")
        except FileNotFoundError as exc:
            raise ObjectNotFoundError(key) from exc

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def delete(self, key: str) -> bool:
        try:
            self._path(key).unlink()
        except FileNotFoundError:
            return False
        return True

    def delete_prefix(self, prefix: str) -> int:
        directory = self._path(prefix)
        if not directory.is_dir():
            return 0
        count = sum(1 for p in directory.rglob("*") if p.is_file())
        try:
            shutil.rmtree(directory)
        except OSError as exc:
            raise StorageError("failed to delete prefix") from exc
        return count

    def iter_objects(self, prefix: str = "") -> Iterator[ObjectInfo]:
        base = self._path(prefix) if prefix else self._root
        if not base.is_dir():
            return
        for path in base.rglob("*"):
            if not path.is_file() or _TMP_DIR in path.relative_to(self._root).parts[:1]:
                continue
            stat = path.stat()
            yield ObjectInfo(
                key=path.relative_to(self._root).as_posix(),
                size=stat.st_size,
                modified_at=datetime.fromtimestamp(stat.st_mtime, UTC),
            )

    def sweep_temporary(self, older_than_s: float) -> int:
        removed = 0
        cutoff = time.time() - older_than_s
        for path in (self._root / _TMP_DIR).iterdir():
            try:
                if path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
            except FileNotFoundError:
                continue  # raced with the writer finishing: fine
        return removed
