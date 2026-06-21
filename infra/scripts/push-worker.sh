#!/bin/bash
# ============================================================
# push-worker.sh ENV VERSION VARIANT
# ------------------------------------------------------------
# Builds + pushes a worker Docker image to GHCR with two tags:
#
#   :ENV-vVERSION   immutable snapshot -- referenced by .env files
#   :ENV            mutable pointer to the latest push for that env
#
# GHCR_USER + GHCR_PAT are read from the REPO-ROOT .env (user-
# level GitHub creds, NOT per-env).  Per-env immutable tag
# references live in infra/envs/$ENV/.env.
#
# Usage:
#   bash infra/scripts/push-worker.sh dev 1.2.0 vast-cycles
#   # -> ghcr.io/saptarshimazumder/pc-rent-vast-worker-cycles:dev-v1.2.0
#   # -> ghcr.io/saptarshimazumder/pc-rent-vast-worker-cycles:dev
#
# After pushing, edit infra/envs/$ENV/.env to point at the new
# immutable tag (e.g. VAST_DOCKER_IMAGE=...:dev-v1.2.0), then
# re-run deploy-server.sh ENV (so Cloud Run picks up the
# updated VAST_DOCKER_IMAGE env var).
#
# Variants: base-cycles, base-eevee, vast-cycles, vast-eevee,
# modal-cycles, community-cycles.
# ============================================================

set -euo pipefail

if [ $# -lt 3 ]; then
    echo "ERROR: usage: $(basename "$0") ENV VERSION VARIANT" >&2
    echo "  ENV     = dev | staging | prod" >&2
    echo "  VERSION = e.g. 1.2.0 (no leading 'v' -- the script adds it)" >&2
    echo "  VARIANT = base-cycles | base-eevee | vast-cycles | vast-eevee | modal-cycles | community-cycles" >&2
    exit 2
fi

ENV="$1"
VERSION="$2"
VARIANT="$3"

if [[ ! "$ENV" =~ ^(dev|staging|prod)$ ]]; then
    echo "ERROR: ENV must be dev, staging, or prod (got '$ENV')" >&2
    exit 2
fi

if [[ ! "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "ERROR: VERSION must be semver MAJOR.MINOR.PATCH (got '$VERSION')" >&2
    exit 2
fi

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ROOT_ENV_FILE="$PROJECT_ROOT/.env"

if [ ! -f "$ROOT_ENV_FILE" ]; then
    echo "ERROR: $ROOT_ENV_FILE not found.  Add GHCR_USER + GHCR_PAT to it." >&2
    exit 1
fi

set -a
# shellcheck disable=SC1090
. "$ROOT_ENV_FILE"
set +a

: "${GHCR_USER:?GHCR_USER must be set in $ROOT_ENV_FILE}"
: "${GHCR_PAT:?GHCR_PAT must be set in $ROOT_ENV_FILE}"

case "$VARIANT" in
    base-cycles)
        IMAGE_NAME="pc-rent-blender-base-cycles"
        DOCKERFILE="$PROJECT_ROOT/blender_base/Dockerfile.cycles"
        ;;
    base-eevee)
        IMAGE_NAME="pc-rent-blender-base-eevee"
        DOCKERFILE="$PROJECT_ROOT/blender_base/Dockerfile.eevee"
        ;;
    vast-cycles)
        IMAGE_NAME="pc-rent-vast-worker-cycles"
        DOCKERFILE="$PROJECT_ROOT/vast_worker/Dockerfile.cycles"
        ;;
    vast-eevee)
        IMAGE_NAME="pc-rent-vast-worker-eevee"
        DOCKERFILE="$PROJECT_ROOT/vast_worker/Dockerfile.eevee"
        ;;
    modal-cycles)
        IMAGE_NAME="pc-rent-modal-worker-cycles"
        DOCKERFILE="$PROJECT_ROOT/modal_worker/Dockerfile.cycles"
        ;;
    community-cycles)
        IMAGE_NAME="pc-rent-community-worker-cycles"
        DOCKERFILE="$PROJECT_ROOT/community_worker/Dockerfile.cycles"
        ;;
    *)
        echo "ERROR: unknown variant '$VARIANT'." >&2
        exit 2
        ;;
esac

IMMUTABLE_TAG="${ENV}-v${VERSION}"
MUTABLE_TAG="${ENV}"
IMAGE_REPO="ghcr.io/${GHCR_USER}/${IMAGE_NAME}"
IMMUTABLE_IMAGE="${IMAGE_REPO}:${IMMUTABLE_TAG}"
MUTABLE_IMAGE="${IMAGE_REPO}:${MUTABLE_TAG}"

echo "==> Login to GHCR"
echo "$GHCR_PAT" | docker login ghcr.io -u "$GHCR_USER" --password-stdin

echo ""
echo "==> Build $IMMUTABLE_IMAGE"
docker build -t "$IMMUTABLE_IMAGE" -f "$DOCKERFILE" "$PROJECT_ROOT"

echo ""
echo "==> Push $IMMUTABLE_IMAGE"
docker push "$IMMUTABLE_IMAGE"

echo ""
echo "==> Tag + push mutable pointer $MUTABLE_IMAGE"
docker tag "$IMMUTABLE_IMAGE" "$MUTABLE_IMAGE"
docker push "$MUTABLE_IMAGE"

echo ""
echo "Done."
echo "  Immutable: $IMMUTABLE_IMAGE"
echo "  Mutable:   $MUTABLE_IMAGE"
echo ""
echo "Next step: update infra/envs/${ENV}/.env so the relevant image"
echo "env var points at the IMMUTABLE tag, then re-run"
echo "  bash infra/scripts/deploy-server.sh ${ENV}"
