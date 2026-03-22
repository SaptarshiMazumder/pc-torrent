#!/bin/bash
set -e

cd "$(dirname "$0")"

echo "Building pcrent-render Docker image..."
docker build -t pcrent-render:latest -f Dockerfile.render .

echo "Exporting image..."
docker save pcrent-render:latest | gzip > pcrent-render.tar.gz

echo "Computing SHA256..."
sha256sum pcrent-render.tar.gz > pcrent-render.sha256

echo "Done."
echo "Size: $(du -h pcrent-render.tar.gz | cut -f1)"
echo "SHA:  $(cat pcrent-render.sha256)"
