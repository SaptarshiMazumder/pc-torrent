#!/bin/bash
set -euo pipefail

# ============================================================
# Forge -- Terraform state-bucket bootstrap (one-time)
# ============================================================
# Creates the GCS bucket that every env's terraform state file
# lives in.  Run this ONCE for the whole org, BEFORE any env's
# first ``terraform init``.  It is idempotent: re-runs are no-ops.
#
# Requirements:
#   * gcloud auth configured for the GCP project
#   * gsutil installed (ships with gcloud)
#
# Usage:
#   bash infra/terraform/bootstrap.sh
# ============================================================

GCP_PROJECT="${GCP_PROJECT:-gen-lang-client-0545494042}"
GCS_BUCKET="${GCS_BUCKET:-pc-rent-tf-state}"
GCS_LOCATION="${GCS_LOCATION:-asia-northeast1}"

echo "==> Forge Terraform state bucket bootstrap"
echo "    project:  $GCP_PROJECT"
echo "    bucket:   gs://$GCS_BUCKET"
echo "    location: $GCS_LOCATION"
echo ""

if gsutil ls -b "gs://$GCS_BUCKET" >/dev/null 2>&1; then
    echo "[ok] gs://$GCS_BUCKET already exists -- nothing to do for create."
else
    echo "[--] creating gs://$GCS_BUCKET ..."
    gsutil mb -p "$GCP_PROJECT" -l "$GCS_LOCATION" "gs://$GCS_BUCKET"
    echo "[ok] created."
fi

echo "[--] enabling versioning (state recovery)..."
gsutil versioning set on "gs://$GCS_BUCKET"
echo "[ok] versioning on."

echo ""
echo "Done.  Next step:"
echo "  cd infra/terraform/envs/dev"
echo "  terraform init"
