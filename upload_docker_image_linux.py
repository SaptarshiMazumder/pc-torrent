#!/usr/bin/env python
"""Upload Linux Docker render image to R2."""

import hashlib
import os
import sys

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError


# Load .env
for line in open("server/.env", encoding="utf-8"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ[k] = v


IMAGE_PATH = os.path.join("server", "docker_linux", "pcrent-render-linux.tar.gz")
REMOTE_IMAGE_KEY = "docker/linux/pcrent-render-linux.tar.gz"
REMOTE_SHA_KEY = "docker/linux/pcrent-render-linux.sha256"


def load_local_sha() -> str:
    digest = hashlib.sha256()
    with open(IMAGE_PATH, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def get_remote_sha(client):
    try:
        response = client.get_object(
            Bucket=os.environ["R2_BUCKET_NAME"],
            Key=REMOTE_SHA_KEY,
        )
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "")
        if error_code in {"404", "NoSuchKey", "NotFound"}:
            return None
        raise

    body = response["Body"].read().decode("utf-8").strip()
    return body.split()[0] if body else None


def remote_image_exists(client) -> bool:
    try:
        client.head_object(
            Bucket=os.environ["R2_BUCKET_NAME"],
            Key=REMOTE_IMAGE_KEY,
        )
        return True
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "")
        if error_code in {"404", "NoSuchKey", "NotFound"}:
            return False
        raise


file_size = os.path.getsize(IMAGE_PATH)
uploaded = [0]


def progress(bytes_amount):
    uploaded[0] += bytes_amount
    pct = int(uploaded[0] / file_size * 100)
    sys.stdout.write(f"\r  uploading... {pct}%")
    sys.stdout.flush()


c = boto3.client(
    "s3",
    endpoint_url=f'https://{os.environ["R2_ACCOUNT_ID"]}.r2.cloudflarestorage.com',
    aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
    aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
    config=Config(signature_version="s3v4"),
    region_name="auto",
)

local_sha = load_local_sha()
remote_sha = get_remote_sha(c)

if remote_sha and remote_sha == local_sha and remote_image_exists(c):
    print("linux image unchanged, skipping upload")
    sys.exit(0)

c.upload_file(
    IMAGE_PATH,
    os.environ["R2_BUCKET_NAME"],
    REMOTE_IMAGE_KEY,
    Callback=progress,
)
print("\nlinux image uploaded")

c.put_object(
    Bucket=os.environ["R2_BUCKET_NAME"],
    Key=REMOTE_SHA_KEY,
    Body=f"{local_sha}  pcrent-render-linux.tar.gz\n".encode("utf-8"),
)
print("linux sha256 uploaded")
