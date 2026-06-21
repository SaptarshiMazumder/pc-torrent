#!/bin/bash
# ============================================================
# destroy.sh ENV
# ------------------------------------------------------------
# Runs ``terraform destroy`` against the env -- removes the
# Cloud Run service, Artifact Registry repo, Neon project,
# Upstash Redis DB, and Cloudflare R2 bucket.  Asks for
# confirmation TWICE before doing anything.
#
# DOES NOT delete:
#   * the Firebase project (manually created; manually deleted)
#   * Modal app (use ``modal app stop pc-rent-render-ENV``)
#   * GHCR worker images (manually delete from GHCR UI)
#   * GCS state bucket (org-wide; bootstrap.sh creates once)
#
# Refuses to run with ENV=prod unless you also pass --i-know.
#
# Usage:
#   bash infra/scripts/destroy.sh dev
#   bash infra/scripts/destroy.sh prod --i-know
# ============================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_env.sh
source "$SCRIPT_DIR/_env.sh" "$1"

GUARD="${2:-}"

if [ "$ENV" = "prod" ] && [ "$GUARD" != "--i-know" ]; then
    echo "ERROR: refusing to destroy prod without --i-know flag." >&2
    echo "       bash $(basename "$0") prod --i-know" >&2
    exit 2
fi

: "${GCP_PROJECT:?GCP_PROJECT must be set in $ENV_FILE}"
: "${ORPHAN_SECRET:?ORPHAN_SECRET must be set in $ENV_FILE}"
: "${VAST_API_KEY:?VAST_API_KEY must be set in $ENV_FILE}"
: "${DATABASE_URL:?DATABASE_URL must be set in $ENV_FILE}"
: "${REDIS_URL:?REDIS_URL must be set in $ENV_FILE}"

export TF_VAR_env="$ENV"
export TF_VAR_gcp_project_id="$GCP_PROJECT"
export TF_VAR_orphan_secret="$ORPHAN_SECRET"
export TF_VAR_vast_api_key="$VAST_API_KEY"
export TF_VAR_database_url="$DATABASE_URL"
export TF_VAR_redis_url="$REDIS_URL"
if [ -n "${GCP_REGION:-}" ]; then
    export TF_VAR_gcp_region="$GCP_REGION"
fi

TF_DIR="$PROJECT_ROOT/infra/terraform/envs/$ENV"

echo ""
echo "==============================================="
echo "  ABOUT TO DESTROY $ENV GCP INFRASTRUCTURE"
echo "==============================================="
echo "  Cloud Run service: $SERVICE_NAME"
echo "  Artifact Registry: $ARTIFACT_REGISTRY_REPO"
echo "  Backup monitor Job + Scheduler"
echo ""
echo "NOT deleted by TF (delete manually if needed):"
echo "  Neon DB, Upstash Redis, R2 bucket, Firebase, Modal, GHCR"
echo "==============================================="
echo ""

read -rp "Type the env name to confirm ('$ENV'): " confirm1
if [ "$confirm1" != "$ENV" ]; then
    echo "Aborted (mismatch)."
    exit 0
fi

read -rp "Type 'destroy' to proceed: " confirm2
if [ "$confirm2" != "destroy" ]; then
    echo "Aborted."
    exit 0
fi

echo ""
echo "==> Terraform destroy ($ENV)"
( cd "$TF_DIR" && terraform init -input=false && terraform destroy -input=false -auto-approve )

echo ""
echo "Done.  $ENV infra is gone."
echo "Manual cleanup left to you:"
echo "  * Firebase project   (Firebase Console -> Project settings -> Delete)"
echo "  * Modal app          (modal app stop pc-rent-render-$ENV)"
echo "  * GHCR worker images (delete from ghcr.io UI if no longer needed)"
