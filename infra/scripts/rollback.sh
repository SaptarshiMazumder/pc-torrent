#!/bin/bash
# ============================================================
# rollback.sh ENV [REVISION]
# ------------------------------------------------------------
# Shifts 100% Cloud Run traffic to a previous revision.  Without
# REVISION, lists the recent revisions so you can pick one.
#
# Cloud Run keeps every past revision -- rollback is a traffic
# shift, not a redeploy.  No image rebuild.
#
# Usage:
#   bash infra/scripts/rollback.sh dev                       # list revisions
#   bash infra/scripts/rollback.sh dev pc-rent-server-v2-dev-00042-xyz
# ============================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_env.sh
source "$SCRIPT_DIR/_env.sh" "$1"

: "${GCP_PROJECT:?GCP_PROJECT must be set in $ENV_FILE}"
: "${GCP_REGION:=asia-northeast1}"
: "${SERVICE_NAME:?SERVICE_NAME must be set in $ENV_FILE}"

REVISION="${2:-}"

if [ -z "$REVISION" ]; then
    echo "==> Recent revisions for $SERVICE_NAME ($ENV):"
    gcloud run revisions list \
        --service "$SERVICE_NAME" \
        --region "$GCP_REGION" \
        --project "$GCP_PROJECT" \
        --format 'table(metadata.name, metadata.creationTimestamp, status.conditions[0].status)' \
        --limit 10
    echo ""
    echo "Pick one and re-run:"
    echo "  bash $(basename "$0") $ENV <revision-name>"
    exit 0
fi

echo "==> Shifting 100% traffic on $SERVICE_NAME -> $REVISION"
gcloud run services update-traffic "$SERVICE_NAME" \
    --to-revisions "${REVISION}=100" \
    --region "$GCP_REGION" \
    --project "$GCP_PROJECT" \
    --quiet

URL="$(gcloud run services describe "$SERVICE_NAME" \
    --region "$GCP_REGION" \
    --project "$GCP_PROJECT" \
    --format 'value(status.url)')"

echo ""
echo "Done.  $ENV now serving revision $REVISION."
echo "  URL: $URL"
