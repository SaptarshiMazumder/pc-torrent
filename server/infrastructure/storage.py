"""
Cloudflare R2 storage layer (S3-compatible).
"""

import io
import os

import boto3
from botocore.config import Config

R2_ACCOUNT_ID = os.environ.get("R2_ACCOUNT_ID", "")
R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID", "")
R2_SECRET_ACCESS_KEY = os.environ.get("R2_SECRET_ACCESS_KEY", "")
R2_BUCKET_NAME = os.environ.get("R2_BUCKET_NAME", "pcrent-files")

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = boto3.client(
            "s3",
            endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
            aws_access_key_id=R2_ACCESS_KEY_ID,
            aws_secret_access_key=R2_SECRET_ACCESS_KEY,
            config=Config(signature_version="s3v4"),
            region_name="auto",
        )
    return _client


def upload_file(key: str, data: bytes, content_type: str = "application/octet-stream"):
    """Upload bytes to R2."""
    _get_client().put_object(
        Bucket=R2_BUCKET_NAME,
        Key=key,
        Body=data,
        ContentType=content_type,
    )


def upload_fileobj(key: str, fileobj, content_type: str = "application/octet-stream"):
    """Upload a file-like object to R2."""
    _get_client().put_object(
        Bucket=R2_BUCKET_NAME,
        Key=key,
        Body=fileobj,
        ContentType=content_type,
    )


def download_file(key: str) -> bytes:
    """Download a file from R2 and return bytes."""
    response = _get_client().get_object(Bucket=R2_BUCKET_NAME, Key=key)
    return response["Body"].read()


def download_fileobj(key: str) -> io.BytesIO:
    """Download a file from R2 and return as BytesIO."""
    data = download_file(key)
    return io.BytesIO(data)


def generate_presigned_url(
    key: str,
    expires_in: int = 3600,
    download_name: str | None = None,
) -> str:
    """Generate a presigned URL for direct download (1 hour default)."""
    params = {"Bucket": R2_BUCKET_NAME, "Key": key}
    if download_name:
        safe_name = download_name.replace('"', "")
        params["ResponseContentDisposition"] = f'attachment; filename="{safe_name}"'

    return _get_client().generate_presigned_url(
        "get_object",
        Params=params,
        ExpiresIn=expires_in,
    )


def generate_presigned_upload_url(key: str, content_type: str = "application/octet-stream", expires_in: int = 3600) -> str:
    """Generate a presigned URL for direct upload to R2."""
    return _get_client().generate_presigned_url(
        "put_object",
        Params={"Bucket": R2_BUCKET_NAME, "Key": key, "ContentType": content_type},
        ExpiresIn=expires_in,
    )


def create_multipart_upload(key: str, content_type: str = "application/octet-stream") -> str:
    """Create a multipart upload session and return UploadId."""
    resp = _get_client().create_multipart_upload(
        Bucket=R2_BUCKET_NAME,
        Key=key,
        ContentType=content_type,
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
    """Generate a presigned URL for uploading one multipart part."""
    return _get_client().generate_presigned_url(
        "upload_part",
        Params={
            "Bucket": R2_BUCKET_NAME,
            "Key": key,
            "UploadId": upload_id,
            "PartNumber": int(part_number),
        },
        ExpiresIn=expires_in,
    )


def complete_multipart_upload(key: str, upload_id: str, parts: list[dict]) -> dict:
    """Complete multipart upload with sorted Parts payload."""
    sorted_parts = sorted(parts, key=lambda p: int(p["PartNumber"]))
    return _get_client().complete_multipart_upload(
        Bucket=R2_BUCKET_NAME,
        Key=key,
        UploadId=upload_id,
        MultipartUpload={"Parts": sorted_parts},
    )


def abort_multipart_upload(key: str, upload_id: str):
    """Abort a multipart upload session."""
    _get_client().abort_multipart_upload(
        Bucket=R2_BUCKET_NAME,
        Key=key,
        UploadId=upload_id,
    )


def delete_file(key: str):
    """Delete a file from R2."""
    _get_client().delete_object(Bucket=R2_BUCKET_NAME, Key=key)


def list_files(prefix: str) -> list[str]:
    """List all keys under a prefix."""
    response = _get_client().list_objects_v2(
        Bucket=R2_BUCKET_NAME, Prefix=prefix
    )
    return [obj["Key"] for obj in response.get("Contents", [])]


def file_exists(key: str) -> bool:
    """Check if a file exists in R2."""
    try:
        _get_client().head_object(Bucket=R2_BUCKET_NAME, Key=key)
        return True
    except Exception:
        return False


def get_file_size(key: str) -> int | None:
    """Return object size in bytes, or None when unavailable."""
    try:
        head = _get_client().head_object(Bucket=R2_BUCKET_NAME, Key=key)
    except Exception:
        return None
    size = head.get("ContentLength")
    if isinstance(size, int):
        return size
    return None
