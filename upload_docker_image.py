#!/usr/bin/env python
"""Upload Docker render image to R2."""
import os
import sys
from botocore.config import Config
import boto3

# Load .env
for line in open('server/.env'):
    line = line.strip()
    if line and not line.startswith('#') and '=' in line:
        k, v = line.split('=', 1)
        os.environ[k] = v

# Build paths
image_path = os.path.join('server', 'docker', 'pcrent-render.tar.gz')
sha_path = os.path.join('server', 'docker', 'pcrent-render.sha256')

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

c.upload_file(image_path, os.environ['R2_BUCKET_NAME'], 'docker/pcrent-render.tar.gz',
    Callback=progress)
print('\nimage uploaded')

if os.path.exists(sha_path):
    c.upload_file(sha_path, os.environ['R2_BUCKET_NAME'], 'docker/pcrent-render.sha256')
    print('sha256 uploaded')
