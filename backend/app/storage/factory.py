from __future__ import annotations

from app.core.config import Settings
from app.storage.base import ObjectStorage
from app.storage.local import LocalObjectStorage
from app.storage.s3 import S3ObjectStorage, create_s3_client


def create_storage(settings: Settings) -> ObjectStorage:
    if settings.storage_backend == "s3":
        return S3ObjectStorage(
            create_s3_client(settings), settings.s3_bucket, prefix=settings.s3_key_prefix
        )
    return LocalObjectStorage(settings.storage_local_root)
