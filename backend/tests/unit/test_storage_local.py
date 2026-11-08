from __future__ import annotations

import hashlib
import io
from pathlib import Path
from typing import BinaryIO, cast

import pytest

from app.storage.base import (
    InvalidKeyError,
    ObjectNotFoundError,
    ObjectTooLargeError,
    validate_key,
)
from app.storage.local import LocalObjectStorage


@pytest.fixture
def storage(tmp_path: Path) -> LocalObjectStorage:
    return LocalObjectStorage(tmp_path / "store")


def test_put_open_roundtrip_and_hash(storage: LocalObjectStorage) -> None:
    data = b"%PDF-1.7 hello" * 1000
    stored = storage.put("u1/d1/file.pdf", io.BytesIO(data))
    assert stored.size == len(data)
    assert stored.sha256 == hashlib.sha256(data).hexdigest()
    with storage.open("u1/d1/file.pdf") as f:
        assert f.read() == data
    assert storage.exists("u1/d1/file.pdf")


def test_overwrite_is_atomic_and_replaces(storage: LocalObjectStorage) -> None:
    storage.put("a/b", io.BytesIO(b"one"))
    storage.put("a/b", io.BytesIO(b"two"))
    with storage.open("a/b") as f:
        assert f.read() == b"two"


def test_max_bytes_enforced_and_no_partial_file(
    storage: LocalObjectStorage, tmp_path: Path
) -> None:
    with pytest.raises(ObjectTooLargeError):
        storage.put("big/obj", io.BytesIO(b"x" * 5000), max_bytes=100)
    assert not storage.exists("big/obj")
    assert list((tmp_path / "store" / ".tmp").iterdir()) == []


def test_failed_stream_leaves_no_object(storage: LocalObjectStorage, tmp_path: Path) -> None:
    class Boom:
        def read(self, size: int = -1) -> bytes:
            raise OSError("disk gone")

    with pytest.raises(OSError, match="disk gone"):
        storage.put("x/y", cast(BinaryIO, Boom()))
    assert not storage.exists("x/y")
    assert list((tmp_path / "store" / ".tmp").iterdir()) == []


UNSAFE_KEYS = [
    "",
    "../escape",
    "a/../../escape",
    "/abs/path",
    "a//b",
    ".hidden",
    "a/.hidden",
    "a/./b",
    "a\\b",
    "C:\\windows\\system32",
    "a/b\x00c",
    "name with space",
    "a/" + "x" * 129,
    "k" * 600,
    "..",
    ".tmp/x",
]


@pytest.mark.parametrize("key", UNSAFE_KEYS)
def test_unsafe_keys_rejected(storage: LocalObjectStorage, key: str) -> None:
    with pytest.raises(InvalidKeyError):
        validate_key(key)
    with pytest.raises(InvalidKeyError):
        storage.put(key, io.BytesIO(b"x"))
    with pytest.raises(InvalidKeyError):
        storage.open(key)


def test_open_missing_raises(storage: LocalObjectStorage) -> None:
    with pytest.raises(ObjectNotFoundError):
        storage.open("nope/nothing")


def test_delete_is_idempotent(storage: LocalObjectStorage) -> None:
    storage.put("d/1", io.BytesIO(b"x"))
    assert storage.delete("d/1") is True
    assert storage.delete("d/1") is False
    assert not storage.exists("d/1")


def test_delete_prefix(storage: LocalObjectStorage) -> None:
    for i in range(3):
        storage.put(f"user1/doc/{i}.pdf", io.BytesIO(b"x"))
    storage.put("user2/doc/0.pdf", io.BytesIO(b"x"))
    assert storage.delete_prefix("user1") == 3
    assert not storage.exists("user1/doc/0.pdf")
    assert storage.exists("user2/doc/0.pdf")
    assert storage.delete_prefix("user1") == 0


def test_delete_prefix_cannot_delete_root(storage: LocalObjectStorage) -> None:
    storage.put("keep/me", io.BytesIO(b"x"))
    with pytest.raises(InvalidKeyError):
        storage.delete_prefix("")
    assert storage.exists("keep/me")
