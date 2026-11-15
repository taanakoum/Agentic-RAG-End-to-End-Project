"""S3-compatible object storage adapter (AWS S3, MinIO, Cloudflare R2, Backblaze B2 ...)."""

from __future__ import annotations

import hashlib
import tempfile
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, BinaryIO, cast

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from app.core.config import Settings
from app.core.logging import get_logger
from app.storage.base import (
    ObjectInfo,
    ObjectNotFoundError,
    ObjectTooLargeError,
    StorageError,
    StoredObject,
    validate_key,
)

if TYPE_CHECKING:
    from types_boto3_s3.client import S3Client
    from types_boto3_s3.type_defs import ObjectIdentifierTypeDef

log = get_logger(__name__)

_SPOOL_MEMORY = 16 * 1024 * 1024
_NOT_FOUND_CODES = {"404", "NoSuchKey", "NotFound"}


class _CountingReader:
    """File-like wrapper that hashes and counts bytes while boto3 streams them out."""

    def __init__(self, stream: BinaryIO, max_bytes: int | None) -> None:
        self._stream = stream
        self._max = max_bytes
        self._digest = hashlib.sha256()
        self.size = 0

    def read(self, size: int = -1) -> bytes:
        chunk = self._stream.read(size)
        if chunk:
            self.size += len(chunk)
            if self._max is not None and self.size > self._max:
                raise ObjectTooLargeError(f"object exceeds {self._max} bytes")
            self._digest.update(chunk)
        return chunk

    @property
    def sha256(self) -> str:
        return self._digest.hexdigest()


def _is_not_found(exc: ClientError) -> bool:
    return str(exc.response.get("Error", {}).get("Code", "")) in _NOT_FOUND_CODES


def create_s3_client(settings: Settings) -> S3Client:
    return boto3.client(  # pyright: ignore[reportUnknownMemberType]
        "s3",
        endpoint_url=settings.s3_endpoint_url or None,
        region_name=settings.s3_region,
        aws_access_key_id=settings.s3_access_key.get_secret_value() or None,
        aws_secret_access_key=settings.s3_secret_key.get_secret_value() or None,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path" if settings.s3_path_style else "auto"},
            connect_timeout=settings.s3_timeout_s,
            read_timeout=settings.s3_timeout_s,
            retries={"max_attempts": 3, "mode": "standard"},
        ),
    )


class S3ObjectStorage:
    def __init__(self, client: S3Client, bucket: str, *, prefix: str = "") -> None:
        self._client = client
        self._bucket = bucket
        self._prefix = prefix

    @property
    def client(self) -> S3Client:
        return self._client

    @property
    def bucket(self) -> str:
        return self._bucket

    def _k(self, key: str) -> str:
        return f"{self._prefix}{validate_key(key)}"

    def put(self, key: str, stream: BinaryIO, *, max_bytes: int | None = None) -> StoredObject:
        reader = _CountingReader(stream, max_bytes)
        try:
            # upload_fileobj switches to multipart for large streams and aborts the multipart
            # upload if reading fails, so a failed put leaves no object and no orphaned parts.
            self._client.upload_fileobj(reader, self._bucket, self._k(key))  # pyright: ignore[reportArgumentType]
        except ObjectTooLargeError:
            raise
        except (ClientError, BotoCoreError) as exc:
            log.error("s3_put_failed", error=type(exc).__name__)
            raise StorageError("failed to store object") from exc
        return StoredObject(key=key, size=reader.size, sha256=reader.sha256)

    def open(self, key: str) -> BinaryIO:
        """Download to a seekable spooled temp file (PDF parsers need seek)."""
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=self._k(key))
        except ClientError as exc:
            if _is_not_found(exc):
                raise ObjectNotFoundError(key) from exc
            raise StorageError("failed to read object") from exc
        except BotoCoreError as exc:
            raise StorageError("failed to read object") from exc
        spool = tempfile.SpooledTemporaryFile(max_size=_SPOOL_MEMORY)  # noqa: SIM115
        try:
            body = response["Body"]
            for chunk in body.iter_chunks(1024 * 1024):
                spool.write(chunk)
            spool.seek(0)
        except Exception:
            spool.close()
            raise
        return cast(BinaryIO, spool)

    def exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self._bucket, Key=self._k(key))
        except ClientError as exc:
            if _is_not_found(exc):
                return False
            raise StorageError("failed to check object") from exc
        except BotoCoreError as exc:  # connection/endpoint failures, like the other operations
            raise StorageError("failed to check object") from exc
        return True

    def delete(self, key: str) -> bool:
        existed = self.exists(key)
        try:
            self._client.delete_object(Bucket=self._bucket, Key=self._k(key))
        except (ClientError, BotoCoreError) as exc:
            raise StorageError("failed to delete object") from exc
        return existed

    def delete_prefix(self, prefix: str) -> int:
        full = f"{self._prefix}{validate_key(prefix)}/"
        deleted = 0
        try:
            paginator = self._client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self._bucket, Prefix=full):
                keys: list[ObjectIdentifierTypeDef] = [
                    {"Key": key} for obj in page.get("Contents", []) if (key := obj.get("Key"))
                ]
                if keys:
                    self._client.delete_objects(
                        Bucket=self._bucket, Delete={"Objects": keys, "Quiet": True}
                    )
                    deleted += len(keys)
        except (ClientError, BotoCoreError) as exc:
            raise StorageError("failed to delete prefix") from exc
        return deleted

    def iter_objects(self, prefix: str = "") -> Iterator[ObjectInfo]:
        full = f"{self._prefix}{validate_key(prefix)}/" if prefix else self._prefix
        try:
            paginator = self._client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self._bucket, Prefix=full):
                for obj in page.get("Contents", []):
                    key, modified = obj.get("Key"), obj.get("LastModified")
                    if key and modified:
                        yield ObjectInfo(
                            key=key.removeprefix(self._prefix),
                            size=int(obj.get("Size", 0)),
                            modified_at=modified.astimezone(UTC),
                        )
        except (ClientError, BotoCoreError) as exc:
            raise StorageError("failed to list objects") from exc

    def sweep_temporary(self, older_than_s: float) -> int:
        """Abort multipart uploads that were started but never completed."""
        cutoff = datetime.now(UTC) - timedelta(seconds=older_than_s)
        aborted = 0
        try:
            paginator = self._client.get_paginator("list_multipart_uploads")
            for page in paginator.paginate(Bucket=self._bucket, Prefix=self._prefix):
                for upload in page.get("Uploads", []):
                    initiated, key, upload_id = (
                        upload.get("Initiated"),
                        upload.get("Key"),
                        upload.get("UploadId"),
                    )
                    if initiated and key and upload_id and initiated < cutoff:
                        self._client.abort_multipart_upload(
                            Bucket=self._bucket, Key=key, UploadId=upload_id
                        )
                        aborted += 1
        except (ClientError, BotoCoreError) as exc:
            raise StorageError("failed to sweep multipart uploads") from exc
        return aborted
