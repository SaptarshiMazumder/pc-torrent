"""File operations — upload, download, delete, exists, list, size."""

from __future__ import annotations

import io

from serverV2.infrastructure.storage.client import get_s3_client, get_bucket


def upload_file(key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
    get_s3_client().put_object(Bucket=get_bucket(), Key=key, Body=data, ContentType=content_type)


def upload_fileobj(key: str, fileobj: io.IOBase, content_type: str = "application/octet-stream") -> None:
    get_s3_client().put_object(Bucket=get_bucket(), Key=key, Body=fileobj, ContentType=content_type)


def download_file(key: str) -> bytes:
    response = get_s3_client().get_object(Bucket=get_bucket(), Key=key)
    return response["Body"].read()


def download_fileobj(key: str) -> io.BytesIO:
    return io.BytesIO(download_file(key))


def delete_file(key: str) -> None:
    get_s3_client().delete_object(Bucket=get_bucket(), Key=key)


def file_exists(key: str) -> bool:
    try:
        get_s3_client().head_object(Bucket=get_bucket(), Key=key)
        return True
    except Exception:
        return False


def list_files(prefix: str) -> list[str]:
    response = get_s3_client().list_objects_v2(Bucket=get_bucket(), Prefix=prefix)
    return [obj["Key"] for obj in response.get("Contents", [])]


def get_file_size(key: str) -> int | None:
    try:
        head = get_s3_client().head_object(Bucket=get_bucket(), Key=key)
    except Exception:
        return None
    size = head.get("ContentLength")
    return size if isinstance(size, int) else None
