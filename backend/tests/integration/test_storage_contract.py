"""The same behavioural contract for every ObjectStorage adapter (local and S3)."""

from __future__ import annotations

import hashlib
import io
import os
import socket
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any, BinaryIO, cast

import boto3
import httpx2
import pytest
from moto.server import ThreadedMotoServer

from app.core.config import Settings, get_settings
from app.storage.base import (
    InvalidKeyError,
    ObjectNotFoundError,
    ObjectStorage,
    ObjectTooLargeError,
    StorageError,
)
from app.storage.factory import create_storage
from app.storage.local import LocalObjectStorage
from app.storage.s3 import S3ObjectStorage, create_s3_client

pytestmark = pytest.mark.integration

S3_ACCESS = "test-access-key"
S3_SECRET = "test-secret-key-0123456789"


@pytest.fixture(scope="module")
def s3_endpoint() -> Iterator[str]:
    """A real HTTP S3 endpoint (moto server). It emulates S3; it is not AWS or MinIO."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
    os.environ.setdefault("AWS_ACCESS_KEY_ID", S3_ACCESS)
    server = ThreadedMotoServer(ip_address="127.0.0.1", port=port, verbose=False)
    server.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.stop()


def s3_settings(endpoint: str, bucket: str) -> Settings:
    return get_settings().model_copy(
        update={
            "storage_backend": "s3",
            "s3_endpoint_url": endpoint,
            "s3_bucket": bucket,
            "s3_access_key": type(get_settings().jwt_secret)(S3_ACCESS),
            "s3_secret_key": type(get_settings().jwt_secret)(S3_SECRET),
        }
    )


@pytest.fixture(params=["local", "s3"])
def storage(request: pytest.FixtureRequest, tmp_path: Path, s3_endpoint: str) -> ObjectStorage:
    if request.param == "local":
        return LocalObjectStorage(tmp_path / "store")
    bucket = f"b-{uuid.uuid4().hex[:12]}"
    settings = s3_settings(s3_endpoint, bucket)
    client = create_s3_client(settings)
    client.create_bucket(Bucket=bucket)
    return S3ObjectStorage(client, bucket)


def test_put_open_roundtrip_hash_and_exists(storage: ObjectStorage) -> None:
    data = b"%PDF-1.7 hello" * 1000
    stored = storage.put("u1/d1/file.pdf", io.BytesIO(data))
    assert stored.size == len(data)
    assert stored.sha256 == hashlib.sha256(data).hexdigest()
    with storage.open("u1/d1/file.pdf") as f:
        assert f.read() == data
        f.seek(0)  # parsers need a seekable stream
        assert f.read(4) == b"%PDF"
    assert storage.exists("u1/d1/file.pdf")
    assert not storage.exists("u1/d1/other.pdf")


def test_overwrite_replaces(storage: ObjectStorage) -> None:
    storage.put("a/b", io.BytesIO(b"one"))
    storage.put("a/b", io.BytesIO(b"two"))
    with storage.open("a/b") as f:
        assert f.read() == b"two"


def test_large_object_uses_multipart_and_survives_intact(storage: ObjectStorage) -> None:
    data = os.urandom(11 * 1024 * 1024)  # above boto3's 8 MiB multipart threshold
    stored = storage.put("big/object.bin", io.BytesIO(data))
    assert stored.size == len(data)
    assert stored.sha256 == hashlib.sha256(data).hexdigest()
    with storage.open("big/object.bin") as f:
        assert hashlib.sha256(f.read()).hexdigest() == stored.sha256


def test_max_bytes_enforced_and_nothing_left_behind(storage: ObjectStorage) -> None:
    with pytest.raises(ObjectTooLargeError):
        storage.put("limit/obj", io.BytesIO(b"x" * 5000), max_bytes=100)
    assert not storage.exists("limit/obj")


def test_oversized_multipart_aborts_without_orphans(storage: ObjectStorage) -> None:
    with pytest.raises(ObjectTooLargeError):
        storage.put(
            "limit/big", io.BytesIO(os.urandom(12 * 1024 * 1024)), max_bytes=9 * 1024 * 1024
        )
    assert not storage.exists("limit/big")
    if isinstance(storage, S3ObjectStorage):
        client = storage.client
        pending = client.list_multipart_uploads(Bucket=storage.bucket)
        assert pending.get("Uploads", []) == []


def test_failing_stream_leaves_no_object(storage: ObjectStorage) -> None:
    class Boom:
        def read(self, size: int = -1) -> bytes:
            raise OSError("disk gone")

    with pytest.raises(Exception, match=r"disk gone|failed to store"):
        storage.put("x/y", cast(BinaryIO, Boom()))
    assert not storage.exists("x/y")


@pytest.mark.parametrize(
    "key", ["", "../escape", "/abs", "a//b", ".hidden", "a/./b", "a\\b", "a b", "k" * 600]
)
def test_unsafe_keys_rejected_everywhere(storage: ObjectStorage, key: str) -> None:
    for call in (
        lambda: storage.put(key, io.BytesIO(b"x")),
        lambda: storage.open(key),
        lambda: storage.exists(key),
        lambda: storage.delete(key),
    ):
        with pytest.raises(InvalidKeyError):
            call()


def test_open_missing_raises(storage: ObjectStorage) -> None:
    with pytest.raises(ObjectNotFoundError):
        storage.open("nope/nothing")


def test_delete_is_idempotent(storage: ObjectStorage) -> None:
    storage.put("d/1", io.BytesIO(b"x"))
    assert storage.delete("d/1") is True
    assert storage.delete("d/1") is False
    assert not storage.exists("d/1")


def test_delete_prefix_is_scoped(storage: ObjectStorage) -> None:
    for i in range(3):
        storage.put(f"user1/doc/{i}.pdf", io.BytesIO(b"x"))
    storage.put("user10/doc/0.pdf", io.BytesIO(b"x"))  # shares a string prefix with user1
    storage.put("user2/doc/0.pdf", io.BytesIO(b"x"))
    assert storage.delete_prefix("user1") == 3
    assert not storage.exists("user1/doc/0.pdf")
    assert storage.exists("user10/doc/0.pdf")
    assert storage.exists("user2/doc/0.pdf")
    assert storage.delete_prefix("user1") == 0


def test_delete_prefix_with_many_objects(storage: ObjectStorage) -> None:
    for i in range(25):
        storage.put(f"many/{i}", io.BytesIO(b"x"))
    assert storage.delete_prefix("many") == 25
    assert not storage.exists("many/0")


def test_key_prefix_isolates_tenants_in_one_bucket(s3_endpoint: str) -> None:
    bucket = f"b-{uuid.uuid4().hex[:12]}"
    client = create_s3_client(s3_settings(s3_endpoint, bucket))
    client.create_bucket(Bucket=bucket)
    one = S3ObjectStorage(client, bucket, prefix="env1/")
    two = S3ObjectStorage(client, bucket, prefix="env2/")
    one.put("k", io.BytesIO(b"1"))
    assert one.exists("k")
    assert not two.exists("k")
    assert client.list_objects_v2(Bucket=bucket).get("Contents", [{}])[0].get("Key") == "env1/k"


def test_missing_bucket_is_a_storage_error_not_a_crash(s3_endpoint: str) -> None:
    storage = S3ObjectStorage(
        create_s3_client(s3_settings(s3_endpoint, "no-such-bucket")), "no-such-bucket"
    )
    with pytest.raises(StorageError):
        storage.put("a/b", io.BytesIO(b"x"))
    with pytest.raises(StorageError):
        storage.open("a/b")
    with pytest.raises(StorageError):
        storage.delete("a/b")
    # S3 HEAD responses carry no body, so a missing bucket is indistinguishable from a missing key
    # here: exists() reports False. Reads, writes and deletes (and the health check) fail loudly.
    assert storage.exists("a/b") is False


def test_unreachable_endpoint_is_a_storage_error() -> None:
    settings = s3_settings("http://127.0.0.1:1", "b")
    storage = create_storage(settings)
    with pytest.raises(StorageError):
        storage.put("a/b", io.BytesIO(b"x"))


def test_s3_settings_require_a_bucket() -> None:
    with pytest.raises(ValueError, match="s3_bucket"):
        Settings.build(
            app_env="test",
            postgres_db="rag_test",
            jwt_secret="x" * 40,
            mfa_encryption_key="dGVzdC1vbmx5LWZlcm5ldC1rZXktMzItYnl0ZXMhISE=",
            storage_backend="s3",
        )


async def test_ready_endpoint_reports_s3_storage_health(
    s3_endpoint: str, client_with_settings: Any
) -> None:
    bucket = f"b-{uuid.uuid4().hex[:12]}"
    settings = s3_settings(s3_endpoint, bucket)
    boto3.client(  # pyright: ignore[reportUnknownMemberType]
        "s3",
        endpoint_url=s3_endpoint,
        aws_access_key_id=S3_ACCESS,
        aws_secret_access_key=S3_SECRET,
        region_name="us-east-1",
    ).create_bucket(Bucket=bucket)
    client: httpx2.AsyncClient = await client_with_settings(settings)
    ok = await client.get("/api/v1/health/ready")
    assert ok.status_code == 200, ok.text
    assert ok.json()["components"]["storage"]["status"] == "ok"

    broken = await client_with_settings(s3_settings(s3_endpoint, "missing-bucket"))
    bad = await broken.get("/api/v1/health/ready")
    assert bad.status_code == 503
    assert bad.json()["components"]["storage"]["status"] == "error"
    assert "missing-bucket" not in bad.text
