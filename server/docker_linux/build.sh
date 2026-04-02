#!/bin/bash
set -e

cd "$(dirname "$0")"

echo "Building pcrent-render-linux Docker image..."
docker build -t pcrent-render-linux:latest -f Dockerfile.render.linux .

echo "Exporting Linux image as flat rootfs (for docker import on RunPod)..."
CID=$(docker create pcrent-render-linux:latest)
docker export "$CID" | gzip > pcrent-render-linux.tar.gz
docker rm "$CID" >/dev/null

echo "Computing SHA256..."
sha256sum pcrent-render-linux.tar.gz > pcrent-render-linux.sha256

echo "Done."
echo "Size: $(du -h pcrent-render-linux.tar.gz | cut -f1)"
echo "SHA:  $(cat pcrent-render-linux.sha256)"