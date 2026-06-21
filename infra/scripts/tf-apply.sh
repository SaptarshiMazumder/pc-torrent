#!/bin/bash
# ============================================================
# tf-apply.sh ENV
# ------------------------------------------------------------
# Runs ``terraform apply`` for the env, then patches the env's
# .env with the auto-fillable outputs (PUBLIC_BACKEND_URL,
# DATABASE_URL, REDIS_URL, R2_*, ARTIFACT_REGISTRY_REPO,
# SERVICE_NAME).  Idempotent -- safe to re-run.
#
# Provisioner secrets (NEON_API_KEY, UPSTASH_*, CLOUDFLARE_*,
# GCP_PROJECT, CLOUDFLARE_ACCOUNT_ID) must be set in the env's
# .env; this script exports them as TF_VAR_* / provider env vars
# before calling terraform.
#
# Usage:
#   bash infra/scripts/tf-apply.sh dev
#   bash infra/scripts/tf-apply.sh staging --yes   # skip apply confirmation
# ============================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_env.sh
source "$SCRIPT_DIR/_env.sh" "$1"

AUTO_APPROVE="${2:-}"
if [ "$AUTO_APPROVE" = "--yes" ]; then
    APPLY_FLAGS="-auto-approve"
else
    APPLY_FLAGS=""
fi

TF_DIR="$PROJECT_ROOT/infra/terraform/envs/$ENV"
if [ ! -d "$TF_DIR" ]; then
    echo "ERROR: $TF_DIR not found." >&2
    exit 1
fi

# --- required env vars (sourced from the env's .env) ---------
# TF only manages GCP resources, so the only provider that needs
# auth here is google -- via ``gcloud auth application-default
# login``, NOT an env var.  The other env vars below feed into
# the backup_monitor module as TF_VAR_*.
: "${GCP_PROJECT:?GCP_PROJECT must be set in $ENV_FILE}"
: "${ORPHAN_SECRET:?ORPHAN_SECRET must be set in $ENV_FILE (consumed by backup_monitor)}"
: "${VAST_API_KEY:?VAST_API_KEY must be set in $ENV_FILE (consumed by backup_monitor)}"
: "${DATABASE_URL:?DATABASE_URL must be set in $ENV_FILE (paste from Neon dashboard; consumed by backup_monitor)}"
: "${REDIS_URL:?REDIS_URL must be set in $ENV_FILE (paste from Upstash dashboard; consumed by backup_monitor)}"

# --- export TF_VAR_* for the per-env composition's variables.tf
export TF_VAR_env="$ENV"
export TF_VAR_gcp_project_id="$GCP_PROJECT"
export TF_VAR_orphan_secret="$ORPHAN_SECRET"
export TF_VAR_vast_api_key="$VAST_API_KEY"
export TF_VAR_database_url="$DATABASE_URL"
export TF_VAR_redis_url="$REDIS_URL"

# Optional region override
if [ -n "${GCP_REGION:-}" ]; then
    export TF_VAR_gcp_region="$GCP_REGION"
fi

# --- run terraform -------------------------------------------
echo "==> Terraform init ($ENV)"
( cd "$TF_DIR" && terraform init -input=false )

echo ""
echo "==> Terraform plan ($ENV)"
( cd "$TF_DIR" && terraform plan -input=false -out=tfplan )

echo ""
echo "==> Terraform apply ($ENV)"
if [ -z "$APPLY_FLAGS" ]; then
    read -rp "Apply the plan above? [y/N] " confirm
    if [[ ! "$confirm" =~ ^[Yy]$ ]]; then
        echo "Aborted."
        rm -f "$TF_DIR/tfplan"
        exit 0
    fi
fi
( cd "$TF_DIR" && terraform apply -input=false tfplan )
rm -f "$TF_DIR/tfplan"

# --- patch env file with auto-fillable outputs ---------------
# Each of these is a key TF can produce -- they replace any
# existing value with the same key in the env file.  Provisioner
# secrets, FIREBASE_*, MODAL_*, VAST_*, OPENAI_*, ANTHROPIC_*,
# *_WORKER_IMAGE are NEVER touched by this script.
echo ""
echo "==> Patching $ENV_FILE with terraform outputs"

PATCHABLE=(
    "PUBLIC_BACKEND_URL:backend_url"
    "SERVICE_NAME:service_name"
    "ARTIFACT_REGISTRY_REPO:artifact_registry_repo"
    "BACKUP_MONITOR_JOB_NAME:backup_monitor_job_name"
    "BACKUP_MONITOR_ARTIFACT_REGISTRY_REPO:backup_monitor_artifact_registry_repo"
)

# Reads each output via ``terraform output -raw NAME``.  Falls
# back to skipping (with a warning) if an output is missing.
TMP_ENV="$(mktemp)"
cp "$ENV_FILE" "$TMP_ENV"

for entry in "${PATCHABLE[@]}"; do
    env_key="${entry%%:*}"
    tf_name="${entry##*:}"
    if ! value="$(cd "$TF_DIR" && terraform output -raw "$tf_name" 2>/dev/null)"; then
        echo "  [skip] $env_key -- terraform output '$tf_name' not available"
        continue
    fi
    # Escape characters that would break sed: / and &.  Replace
    # the existing assignment, or append if absent.
    escaped="${value//\\/\\\\}"
    escaped="${escaped//&/\\&}"
    escaped="${escaped//\//\\/}"
    if grep -q "^${env_key}=" "$TMP_ENV"; then
        sed -i.bak "s/^${env_key}=.*/${env_key}=${escaped}/" "$TMP_ENV"
        rm -f "$TMP_ENV.bak"
    else
        printf '\n%s=%s\n' "$env_key" "$value" >> "$TMP_ENV"
    fi
    echo "  [ok]   $env_key"
done

mv "$TMP_ENV" "$ENV_FILE"

echo ""
echo "Done.  $ENV_FILE now reflects the live infra."
echo "Next: bash infra/scripts/deploy-server.sh $ENV"
