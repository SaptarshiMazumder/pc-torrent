#!/bin/bash
# Deploy the backup monitor as a Cloud Run Job + Cloud Scheduler trigger.
# The Job runs ONE scan tick and exits; Scheduler invokes it every minute.
# Cost: ~$3-5/mo vs ~$65/mo for an always-on service.

set -e

THIS_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$THIS_DIR/.." && pwd)"
ENV_FILE="$PROJECT_ROOT/serverV2/.env"

if [ ! -f "$ENV_FILE" ]; then
    echo "ERROR: $ENV_FILE not found.  Backup monitor reuses serverV2's .env."
    exit 1
fi

# Pull individual keys from .env without sourcing it.  Sourcing breaks on
# values like FIREBASE_SERVICE_ACCOUNT_JSON that contain unquoted JSON.
read_env() {
    local key="$1"
    local line
    line="$(grep -E "^${key}=" "$ENV_FILE" | head -1)"
    if [ -z "$line" ]; then
        return 1
    fi
    printf '%s' "${line#${key}=}"
}

DATABASE_URL="$(read_env DATABASE_URL)"   || { echo "ERROR: DATABASE_URL not in $ENV_FILE"; exit 1; }
REDIS_URL="$(read_env REDIS_URL)"         || { echo "ERROR: REDIS_URL not in $ENV_FILE"; exit 1; }
ORPHAN_SECRET="$(read_env ORPHAN_SECRET)" || { echo "ERROR: ORPHAN_SECRET not in $ENV_FILE (shared with serverV2)"; exit 1; }

REGION="${REGION:-asia-northeast1}"
JOB_NAME="${JOB_NAME:-pcrent-backup-monitor}"
SCHEDULER_NAME="${SCHEDULER_NAME:-pcrent-backup-monitor-tick}"
SCHEDULE="${SCHEDULE:-* * * * *}"
ORCHESTRATOR_URL="${ORCHESTRATOR_URL:-https://pcrent-server-v2-930713698987.asia-northeast1.run.app}"

PROJECT_ID="$(gcloud config get-value project 2>/dev/null)"
[ -n "$PROJECT_ID" ] || { echo "ERROR: gcloud project not set (run 'gcloud config set project ...')"; exit 1; }
PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
SCHEDULER_SA="${SCHEDULER_SA:-${PROJECT_NUMBER}-compute@developer.gserviceaccount.com}"

echo "-> Project:      $PROJECT_ID"
echo "   Region:       $REGION"
echo "   Job:          $JOB_NAME"
echo "   Scheduler:    $SCHEDULER_NAME ($SCHEDULE)"
echo "   Service acct: $SCHEDULER_SA"
echo ""

# --- 1. Deploy / update the Cloud Run Job ---
echo "-> Deploying Cloud Run Job"
gcloud run jobs deploy "$JOB_NAME" \
    --source "$THIS_DIR" \
    --region "$REGION" \
    --memory 512Mi \
    --cpu 1 \
    --max-retries 1 \
    --task-timeout 60s \
    --set-env-vars "DATABASE_URL=$DATABASE_URL" \
    --set-env-vars "REDIS_URL=$REDIS_URL" \
    --set-env-vars "ORCHESTRATOR_URL=$ORCHESTRATOR_URL" \
    --set-env-vars "ORPHAN_SECRET=$ORPHAN_SECRET"

# --- 2. Grant the scheduler service account permission to invoke the job ---
echo ""
echo "-> Granting roles/run.invoker to $SCHEDULER_SA on $JOB_NAME"
gcloud run jobs add-iam-policy-binding "$JOB_NAME" \
    --region "$REGION" \
    --member "serviceAccount:$SCHEDULER_SA" \
    --role "roles/run.invoker" \
    --quiet

# --- 3. Create / update the Cloud Scheduler trigger ---
JOB_URI="https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT_ID}/jobs/${JOB_NAME}:run"
echo ""
echo "-> Configuring Cloud Scheduler trigger"
if gcloud scheduler jobs describe "$SCHEDULER_NAME" --location "$REGION" >/dev/null 2>&1; then
    gcloud scheduler jobs update http "$SCHEDULER_NAME" \
        --location "$REGION" \
        --schedule "$SCHEDULE" \
        --uri "$JOB_URI" \
        --http-method POST \
        --oauth-service-account-email "$SCHEDULER_SA"
else
    gcloud scheduler jobs create http "$SCHEDULER_NAME" \
        --location "$REGION" \
        --schedule "$SCHEDULE" \
        --uri "$JOB_URI" \
        --http-method POST \
        --oauth-service-account-email "$SCHEDULER_SA"
fi

echo ""
echo "Deploy complete."
echo ""
echo "Trigger a one-off run:"
echo "  gcloud run jobs execute $JOB_NAME --region $REGION"
echo ""
echo "List recent executions:"
echo "  gcloud run jobs executions list --job $JOB_NAME --region $REGION"
