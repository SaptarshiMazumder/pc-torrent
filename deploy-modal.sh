#!/bin/bash
set -e

# Wrapper around `modal deploy modal_worker/app.py` that loads the env
# vars Modal's app.py needs (MODAL_WORKER_IMAGE specifically) from
# serverV2/.env so the operator doesn't have to remember the inline
# `MODAL_WORKER_IMAGE=...` prefix.  Mirrors the env-loading shape of
# deploy.sh and push-worker.sh.

PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="$PROJECT_ROOT/serverV2/.env"

if [ ! -f "$ENV_FILE" ]; then
    echo "ERROR: $ENV_FILE not found"
    exit 1
fi

while IFS='=' read -r key value; do
    key="${key%$'\r'}"
    value="${value%$'\r'}"
    [[ -z "$key" || "$key" =~ ^# ]] && continue
    export "$key=$value"
done < "$ENV_FILE"

if [ -z "$MODAL_WORKER_IMAGE_CYCLES" ]; then
    echo "ERROR: MODAL_WORKER_IMAGE_CYCLES not set in $ENV_FILE"
    exit 1
fi

echo "-> MODAL_WORKER_IMAGE_CYCLES=$MODAL_WORKER_IMAGE_CYCLES"
echo "-> Deploying modal_worker/app.py ..."
modal deploy "$PROJECT_ROOT/modal_worker/app.py"
