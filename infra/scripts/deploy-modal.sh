#!/bin/bash
# ============================================================
# deploy-modal.sh ENV
# ------------------------------------------------------------
# Wraps ``modal deploy modal_worker/app.py`` with the env's
# MODAL_APP_NAME + MODAL_WORKER_IMAGE_CYCLES + MODAL_TOKEN_*
# sourced from infra/envs/$ENV/.env.
#
# DEPENDENCY: modal_worker/app.py must read ``MODAL_APP_NAME``
# from env (currently hardcoded to ``pc-rent-render``).  Without
# that change, every env's deploy clobbers the same Modal app.
# See "outstanding modal_worker change" note at the bottom of
# infra/README.md.
#
# Usage:
#   bash infra/scripts/deploy-modal.sh dev
# ============================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_env.sh
source "$SCRIPT_DIR/_env.sh" "$1"

: "${MODAL_APP_NAME:?MODAL_APP_NAME must be set in $ENV_FILE (e.g. pc-rent-render-dev)}"
: "${MODAL_WORKER_IMAGE_CYCLES:?MODAL_WORKER_IMAGE_CYCLES must be set in $ENV_FILE}"
: "${MODAL_TOKEN_ID:?MODAL_TOKEN_ID must be set in $ENV_FILE}"
: "${MODAL_TOKEN_SECRET:?MODAL_TOKEN_SECRET must be set in $ENV_FILE}"

echo "==> Deploying Modal app for $ENV"
echo "    app name: $MODAL_APP_NAME"
echo "    image:    $MODAL_WORKER_IMAGE_CYCLES"

cd "$PROJECT_ROOT"
modal deploy modal_worker/app.py

echo ""
echo "Done.  $ENV Modal app deployed."
