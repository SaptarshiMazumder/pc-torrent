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
    # Shared Blender base.  Build + push these FIRST -- the per-fleet
    # variants below FROM these images.  Tag with the Blender version
    # (e.g. 5.0.1).
    base-cycles)
        IMAGE_NAME="pcrent-blender-base-cycles"
        DOCKERFILE="$PROJECT_ROOT/blender_base/Dockerfile.cycles"
        ;;
    base-eevee)
        IMAGE_NAME="pcrent-blender-base-eevee"
        DOCKERFILE="$PROJECT_ROOT/blender_base/Dockerfile.eevee"
        ;;
    # Vast worker -- env-driven container, runs vast_worker/scripts/handler.py.
    vast-cycles)
        IMAGE_NAME="pcrent-vast-worker-cycles"
        DOCKERFILE="$PROJECT_ROOT/vast_worker/Dockerfile.cycles"
        ;;
    vast-eevee)
        IMAGE_NAME="pcrent-vast-worker-eevee"
        DOCKERFILE="$PROJECT_ROOT/vast_worker/Dockerfile.eevee"
        ;;
    # Modal worker -- self-contained image pulled by Modal at function-spawn
    # time.  No more copy_local_dir mounting at deploy time.
    modal-cycles)
        IMAGE_NAME="pcrent-modal-worker-cycles"
        DOCKERFILE="$PROJECT_ROOT/modal_worker/Dockerfile.cycles"
        ;;
    # Community worker -- thin renderer for community PCs.  Agent
    # (running on user's PC) orchestrates I/O; this image just runs
    # Blender against /input and writes to /output.
    community-cycles)
        IMAGE_NAME="pcrent-community-worker-cycles"
        DOCKERFILE="$PROJECT_ROOT/community_worker/Dockerfile.cycles"
        ;;
    *)
        echo "ERROR: unknown variant '$VARIANT'."
        echo "Usage: $0 <tag> <variant>"
        echo ""
        echo "Variants:"
        echo "  base-cycles, base-eevee           shared Blender base (build first)"
        echo "  vast-cycles, vast-eevee           Vast worker (FROM blender-base)"
        echo "  modal-cycles                      Modal worker (FROM blender-base)"
        echo "  community-cycles                  Community worker (FROM blender-base)"
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
