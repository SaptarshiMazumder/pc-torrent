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
        DOCKERFILE="$PROJECT_ROOT/runpod_worker/Dockerfile"
        ;;
    eevee)
        IMAGE_NAME="pcrent-worker-eevee"
        DOCKERFILE="$PROJECT_ROOT/runpod_worker/Dockerfile.eevee"
        ;;
    *)
        echo "ERROR: unknown variant '$VARIANT'.  Expected 'cycles' or 'eevee'."
        echo "Usage: $0 <tag> [cycles|eevee]"
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
