#!/usr/bin/env python
"""Upload Windows Docker render image to R2."""
import hashlib
import os
import sys

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

# Load .env
for line in open('server/.env'):
    line = line.strip()
    if line and not line.startswith('#') and '=' in line:
        k, v = line.split('=', 1)
        os.environ[k] = v

# Build paths
image_path = os.path.join('server', 'docker', 'pcrent-render.tar.gz')

def load_local_sha():
    digest = hashlib.sha256()
    with open(image_path, 'rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def get_remote_sha(client):
    try:
        response = client.get_object(
            Bucket=os.environ['R2_BUCKET_NAME'],
            Key='docker/pcrent-render.sha256',
        )
    except ClientError as exc:
        error_code = exc.response.get('Error', {}).get('Code', '')
        if error_code in {'404', 'NoSuchKey', 'NotFound'}:
            return None
        raise

    body = response['Body'].read().decode('utf-8').strip()
    return body.split()[0] if body else None


def remote_image_exists(client):
    try:
        client.head_object(
            Bucket=os.environ['R2_BUCKET_NAME'],
            Key='docker/pcrent-render.tar.gz',
        )
        return True
    except ClientError as exc:
        error_code = exc.response.get('Error', {}).get('Code', '')
        if error_code in {'404', 'NoSuchKey', 'NotFound'}:
            return False
        raise


file_size = os.path.getsize(image_path)
uploaded = [0]

def progress(bytes_amount):
    uploaded[0] += bytes_amount
    pct = int(uploaded[0] / file_size * 100)
    sys.stdout.write(f'\r  uploading... {pct}%')
    sys.stdout.flush()

c = boto3.client('s3',
    endpoint_url=f'https://{os.environ["R2_ACCOUNT_ID"]}.r2.cloudflarestorage.com',
    aws_access_key_id=os.environ['R2_ACCESS_KEY_ID'],
    aws_secret_access_key=os.environ['R2_SECRET_ACCESS_KEY'],
    config=Config(signature_version='s3v4'), region_name='auto')

local_sha = load_local_sha()
remote_sha = get_remote_sha(c)

if remote_sha and remote_sha == local_sha and remote_image_exists(c):
    print('image unchanged, skipping upload')
    sys.exit(0)

c.upload_file(image_path, os.environ['R2_BUCKET_NAME'], 'docker/pcrent-render.tar.gz',
    Callback=progress)
print('\nimage uploaded')

c.put_object(
    Bucket=os.environ['R2_BUCKET_NAME'],
    Key='docker/pcrent-render.sha256',
    Body=f'{local_sha}  pcrent-render.tar.gz\n'.encode('utf-8'),
)
print('sha256 uploaded')
