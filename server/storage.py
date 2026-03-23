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


def generate_presigned_url(key: str, expires_in: int = 3600) -> str:
    """Generate a presigned URL for direct download (1 hour default)."""
    return _get_client().generate_presigned_url(
        "get_object",
        Params={"Bucket": R2_BUCKET_NAME, "Key": key},
        ExpiresIn=expires_in,
    )


def generate_presigned_upload_url(key: str, content_type: str = "application/octet-stream", expires_in: int = 3600) -> str:
    """Generate a presigned URL for direct upload to R2."""
    return _get_client().generate_presigned_url(
        "put_object",
        Params={"Bucket": R2_BUCKET_NAME, "Key": key, "ContentType": content_type},
        ExpiresIn=expires_in,
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
