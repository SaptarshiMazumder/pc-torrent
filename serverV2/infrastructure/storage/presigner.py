"""Presigned URL generation for download and single-part upload."""

from __future__ import annotations

from serverV2.infrastructure.storage.client import get_s3_client, get_bucket


def generate_presigned_url(
    key: str,
    expires_in: int = 3600,
    download_name: str | None = None,
) -> str:
    params: dict = {"Bucket": get_bucket(), "Key": key}
    if download_name:
        safe_name = download_name.replace('"', "")
        params["ResponseContentDisposition"] = f'attachment; filename="{safe_name}"'
    return get_s3_client().generate_presigned_url(
        "get_object", Params=params, ExpiresIn=expires_in,
    )


def generate_presigned_upload_url(
    key: str,
    content_type: str = "application/octet-stream",
    expires_in: int = 3600,
) -> str:
    return get_s3_client().generate_presigned_url(
        "put_object",
        Params={"Bucket": get_bucket(), "Key": key, "ContentType": content_type},
        ExpiresIn=expires_in,
    )
