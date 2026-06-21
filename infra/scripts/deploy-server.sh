#!/bin/bash
# ============================================================
# deploy-server.sh ENV
# ------------------------------------------------------------
# Builds the serverV2 image via Cloud Build, pushes it to the
# env's Artifact Registry repo, then ``gcloud run deploy`` with
# every var in infra/envs/$ENV/.env injected.  New revision is
# created on every run; Cloud Run shifts traffic atomically.
#
# Pre-reqs (already populated by tf-apply.sh):
#   GCP_PROJECT             -- GCP project ID
#   GCP_REGION              -- Cloud Run + Artifact Registry region
#   SERVICE_NAME            -- Cloud Run service name (e.g. pc-rent-server-v2-dev)
#   ARTIFACT_REGISTRY_REPO  -- e.g. asia-northeast1-docker.pkg.dev/PROJECT/pc-rent-dev
#
# Pre-reqs (already populated by you when setting up the env):
#   FIREBASE_SERVICE_ACCOUNT_JSON  -- inline JSON string
#   OPENAI_API_KEY, etc.
#
# Usage:
#   bash infra/scripts/deploy-server.sh dev
# ============================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_env.sh
source "$SCRIPT_DIR/_env.sh" "$1"

: "${GCP_PROJECT:?GCP_PROJECT must be set (run tf-apply.sh first)}"
: "${GCP_REGION:=asia-northeast1}"
: "${SERVICE_NAME:?SERVICE_NAME must be set (run tf-apply.sh first)}"
: "${ARTIFACT_REGISTRY_REPO:?ARTIFACT_REGISTRY_REPO must be set (run tf-apply.sh first)}"
: "${FIREBASE_SERVICE_ACCOUNT_JSON:?FIREBASE_SERVICE_ACCOUNT_JSON must be set in $ENV_FILE}"

IMAGE_URI="${ARTIFACT_REGISTRY_REPO}/${SERVICE_NAME}:$(git -C "$PROJECT_ROOT" rev-parse --short HEAD)"

echo "==> Building serverV2 image"
echo "    image: $IMAGE_URI"
( cd "$PROJECT_ROOT/serverV2" && gcloud builds submit \
    --tag "$IMAGE_URI" \
    --project "$GCP_PROJECT" \
    --quiet )

# --- Convert env file -> Cloud Run YAML ----------------------
# Cloud Run accepts ``--env-vars-file`` only as YAML.  Single
# source of truth: every var in infra/envs/$ENV/.env reaches
# Cloud Run, no allowlist drift.
ENV_YAML="$(mktemp)"
trap 'rm -f "$ENV_YAML"' EXIT

echo ""
echo "==> Converting $ENV_FILE -> Cloud Run env YAML"
# Use the flattened env file from _env.sh, not the raw one -- the raw
# file has pretty-printed multi-line JSON (FIREBASE_SERVICE_ACCOUNT_JSON)
# that a line-by-line parse would truncate to just the opening `{`.
python - "$ENV_FILE_FLAT" "$ENV_YAML" <<'PY'
import json, sys
src, dst = sys.argv[1], sys.argv[2]
out = {}
with open(src, encoding="utf-8") as f:
    for raw in f:
        line = raw.rstrip("\r\n").strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v
with open(dst, "w", encoding="utf-8") as f:
    for k, v in out.items():
        f.write(f"{k}: {json.dumps(v)}\n")
PY

# --- Deploy --------------------------------------------------
echo ""
echo "==> Deploying to Cloud Run"
echo "    service: $SERVICE_NAME"
echo "    region:  $GCP_REGION"
gcloud run deploy "$SERVICE_NAME" \
    --image "$IMAGE_URI" \
    --platform managed \
    --region "$GCP_REGION" \
    --project "$GCP_PROJECT" \
    --allow-unauthenticated \
    --memory 2Gi \
    --min-instances 1 \
    --no-cpu-throttling \
    --concurrency=80 \
    --cpu=2 \
    --env-vars-file "$ENV_YAML" \
    --quiet

BACKEND_URL="$(gcloud run services describe "$SERVICE_NAME" \
    --platform managed \
    --region "$GCP_REGION" \
    --project "$GCP_PROJECT" \
    --format 'value(status.url)')"

# --- Health check --------------------------------------------
echo ""
echo "==> Health check ($BACKEND_URL)"
if curl -sf --max-time 10 "$BACKEND_URL/" | grep -q 'running'; then
    echo "[ok] backend is live"
else
    echo "[warn] health check failed -- backend may still be warming up"
fi

echo ""
echo "Done.  $ENV deployed."
echo "  URL:   $BACKEND_URL"
echo "  Image: $IMAGE_URI"
