#!/bin/bash
set -e

PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="$PROJECT_ROOT/.env"

if [ ! -f "$ENV_FILE" ]; then
    echo "ERROR: .env not found. Add GHCR_PAT and GHCR_USER to $ENV_FILE"
    exit 1
fi

# Load .env
while IFS='=' read -r key value; do
    key="${key%$'\r'}"
    value="${value%$'\r'}"
    [[ -z "$key" || "$key" =~ ^# ]] && continue
    export "$key=$value"
done < "$ENV_FILE"

if [ -z "$GHCR_PAT" ] || [ -z "$GHCR_USER" ]; then
    echo "ERROR: GHCR_PAT and GHCR_USER must be set in .env"
    exit 1
fi

TAG="${1:-latest}"
VARIANT="${2:-cycles}"

case "$VARIANT" in
    cycles)
        IMAGE_NAME="pcrent-worker"
        DOCKERFILE="$PROJECT_ROOT/cloud_worker/Dockerfile.cycles"
        ;;
    eevee)
        IMAGE_NAME="pcrent-worker-eevee"
        DOCKERFILE="$PROJECT_ROOT/cloud_worker/Dockerfile.eevee"
        ;;
    community-cycles)
        # Thin renderer for community PCs (Cycles) — agent orchestrates
        # I/O, this image just runs Blender against /input and /output
        # mounts.
        IMAGE_NAME="pcrent-community-worker-cycles"
        DOCKERFILE="$PROJECT_ROOT/community_worker/Dockerfile.cycles"
        ;;
    community-eevee)
        # Same as community-cycles but with libegl1-mesa / libgles2-mesa
        # for EEVEE's OpenGL pipeline.  Larger image; only pulled by
        # agents when they claim an EEVEE job.
        IMAGE_NAME="pcrent-community-worker-eevee"
        DOCKERFILE="$PROJECT_ROOT/community_worker/Dockerfile.eevee"
        ;;
    *)
        echo "ERROR: unknown variant '$VARIANT'.  Expected one of: cycles, eevee, community-cycles, community-eevee."
        echo "Usage: $0 <tag> [cycles|eevee|community-cycles|community-eevee]"
        exit 1
        ;;
esac

IMAGE="ghcr.io/${GHCR_USER}/${IMAGE_NAME}:${TAG}"

echo "-> Logging in to GHCR..."
echo "$GHCR_PAT" | docker login ghcr.io -u "$GHCR_USER" --password-stdin

echo "-> Building $IMAGE (variant=$VARIANT) ..."
docker build -t "$IMAGE" -f "$DOCKERFILE" "$PROJECT_ROOT"

echo "-> Pushing $IMAGE ..."
docker push "$IMAGE"

echo ""
echo "Done. Image: $IMAGE"
