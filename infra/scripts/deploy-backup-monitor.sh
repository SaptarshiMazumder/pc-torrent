#!/bin/bash
# ============================================================
# deploy-backup-monitor.sh ENV
# ------------------------------------------------------------
# Builds the backup_monitor image via Cloud Build, pushes it to
# the env's dedicated Artifact Registry repo, then runs
# ``gcloud run jobs update`` to swap the running image AND
# refresh every env var from infra/envs/$ENV/.env.
#
# The Cloud Run Job + Scheduler trigger are created by tf-apply.sh
# (with a placeholder image).  This script makes the Job actually
# do something useful.  Idempotent -- safe to re-run on every
# backup_monitor code change.
#
# Pre-reqs (already populated by tf-apply.sh):
#   GCP_PROJECT
#   GCP_REGION
#   BACKUP_MONITOR_JOB_NAME           e.g. pc-rent-backup-monitor-dev
#   BACKUP_MONITOR_ARTIFACT_REGISTRY_REPO  asia-northeast1-docker.pkg.dev/.../pc-rent-backup-monitor-dev
#   PUBLIC_BACKEND_URL                 wired into ORCHESTRATOR_URL
#   DATABASE_URL, REDIS_URL, ORPHAN_SECRET, VAST_API_KEY
#
# Usage:
#   bash infra/scripts/deploy-backup-monitor.sh dev
# ============================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_env.sh
source "$SCRIPT_DIR/_env.sh" "$1"

: "${GCP_PROJECT:?GCP_PROJECT must be set (run tf-apply.sh first)}"
: "${GCP_REGION:=asia-northeast1}"
: "${BACKUP_MONITOR_JOB_NAME:?BACKUP_MONITOR_JOB_NAME must be set (run tf-apply.sh first)}"
: "${BACKUP_MONITOR_ARTIFACT_REGISTRY_REPO:?BACKUP_MONITOR_ARTIFACT_REGISTRY_REPO must be set (run tf-apply.sh first)}"
: "${PUBLIC_BACKEND_URL:?PUBLIC_BACKEND_URL must be set (run tf-apply.sh first)}"
: "${DATABASE_URL:?DATABASE_URL must be set in $ENV_FILE}"
: "${REDIS_URL:?REDIS_URL must be set in $ENV_FILE}"
: "${ORPHAN_SECRET:?ORPHAN_SECRET must be set in $ENV_FILE}"
: "${VAST_API_KEY:?VAST_API_KEY must be set in $ENV_FILE}"

IMAGE_URI="${BACKUP_MONITOR_ARTIFACT_REGISTRY_REPO}/${BACKUP_MONITOR_JOB_NAME}:$(git -C "$PROJECT_ROOT" rev-parse --short HEAD)"

echo "==> Building backup_monitor image"
echo "    image: $IMAGE_URI"
( cd "$PROJECT_ROOT/backup_monitor" && gcloud builds submit \
    --tag "$IMAGE_URI" \
    --project "$GCP_PROJECT" \
    --quiet )

# --- Swap image + refresh env vars on the Job --------------
# ``gcloud run jobs update`` reuses every other Job setting that
# TF already configured (memory, cpu, timeout, retries, schedule,
# IAM).  This script only touches what changes per-deploy: the
# container image + the env-var values that came from .env.
echo ""
echo "==> Updating Cloud Run Job ($BACKUP_MONITOR_JOB_NAME)"
echo "    orchestrator_url: $PUBLIC_BACKEND_URL"

# Pass tuning knobs through if set (else the Job keeps the
# values TF baked in at apply time).
HTTP_TIMEOUT_SEC="${HTTP_TIMEOUT_SEC:-10}"
VAST_GHOST_MIN_AGE_SEC="${VAST_GHOST_MIN_AGE_SEC:-120}"
MODAL_TERMINAL_WINDOW_HOURS="${MODAL_TERMINAL_WINDOW_HOURS:-24}"
MODAL_TERMINAL_MAX_ROWS="${MODAL_TERMINAL_MAX_ROWS:-200}"

gcloud run jobs update "$BACKUP_MONITOR_JOB_NAME" \
    --image "$IMAGE_URI" \
    --region "$GCP_REGION" \
    --project "$GCP_PROJECT" \
    --set-env-vars "ENV_NAME=$ENV,DATABASE_URL=$DATABASE_URL,REDIS_URL=$REDIS_URL,ORCHESTRATOR_URL=$PUBLIC_BACKEND_URL,ORPHAN_SECRET=$ORPHAN_SECRET,VAST_API_KEY=$VAST_API_KEY,HTTP_TIMEOUT_SEC=$HTTP_TIMEOUT_SEC,VAST_GHOST_MIN_AGE_SEC=$VAST_GHOST_MIN_AGE_SEC,MODAL_TERMINAL_WINDOW_HOURS=$MODAL_TERMINAL_WINDOW_HOURS,MODAL_TERMINAL_MAX_ROWS=$MODAL_TERMINAL_MAX_ROWS" \
    --quiet

echo ""
echo "Done.  $ENV backup_monitor deployed."
echo "  Job:   $BACKUP_MONITOR_JOB_NAME"
echo "  Image: $IMAGE_URI"
echo ""
echo "Trigger a one-off tick to smoke-test:"
echo "  gcloud run jobs execute $BACKUP_MONITOR_JOB_NAME --region $GCP_REGION --project $GCP_PROJECT"
echo ""
echo "Watch executions:"
echo "  gcloud run jobs executions list --job $BACKUP_MONITOR_JOB_NAME --region $GCP_REGION --project $GCP_PROJECT"
