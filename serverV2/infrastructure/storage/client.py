"""Lazy-initialized boto3 S3 client for Cloudflare R2."""

from __future__ import annotations

import os

import boto3
from botocore.config import Config

R2_ACCOUNT_ID = os.environ.get("R2_ACCOUNT_ID", "")
R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID", "")
R2_SECRET_ACCESS_KEY = os.environ.get("R2_SECRET_ACCESS_KEY", "")
R2_BUCKET_NAME = os.environ.get("R2_BUCKET_NAME", "pcrent-files")
# Separate bucket for worker stdout logs so retention lifecycle rules can
# be set independently of user-visible frame outputs.  Env: pc-rent-logs-
# {env}.  Falls back to R2_BUCKET_NAME so a partially-configured deploy
# still writes SOMEWHERE identifiable rather than silently no-op.
R2_LOGS_BUCKET_NAME = os.environ.get(
    "R2_LOGS_BUCKET_NAME", R2_BUCKET_NAME,
)

_client = None


def get_s3_client():
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


def get_bucket() -> str:
    return R2_BUCKET_NAME


def get_logs_bucket() -> str:
    return R2_LOGS_BUCKET_NAME
