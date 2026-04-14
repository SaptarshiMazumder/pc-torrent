"""Multipart upload lifecycle — create, part URL, complete, abort."""

from __future__ import annotations

from serverV2.infrastructure.storage.client import get_s3_client, get_bucket


def create_multipart_upload(key: str, content_type: str = "application/octet-stream") -> str:
    resp = get_s3_client().create_multipart_upload(
        Bucket=get_bucket(), Key=key, ContentType=content_type,
    )
    upload_id = resp.get("UploadId")
    if not upload_id:
        raise RuntimeError("R2 did not return UploadId for multipart upload")
    return upload_id


def generate_presigned_upload_part_url(
    key: str,
    upload_id: str,
    part_number: int,
    expires_in: int = 43200,
) -> str:
    return get_s3_client().generate_presigned_url(
        "upload_part",
        Params={
            "Bucket": get_bucket(),
            "Key": key,
            "UploadId": upload_id,
            "PartNumber": int(part_number),
        },
        ExpiresIn=expires_in,
    )


def complete_multipart_upload(key: str, upload_id: str, parts: list[dict]) -> dict:
    sorted_parts = sorted(parts, key=lambda p: int(p["PartNumber"]))
    return get_s3_client().complete_multipart_upload(
        Bucket=get_bucket(),
        Key=key,
        UploadId=upload_id,
        MultipartUpload={"Parts": sorted_parts},
    )


def abort_multipart_upload(key: str, upload_id: str) -> None:
    get_s3_client().abort_multipart_upload(
        Bucket=get_bucket(), Key=key, UploadId=upload_id,
    )
