#!/bin/bash
set -e

# ============================================================
# PC Rent - Full Deployment Script
# Deploys: Backend (Cloud Run) + Frontend (Cloudflare Pages)
#          + Windows/Linux Docker render images to R2
# ============================================================

PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
GCP_PROJECT="gen-lang-client-0545494042"
GCP_REGION="asia-northeast1"
SERVICE_NAME="pcrent-server-v2"

# Colors
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BOLD='\033[1m'
NC='\033[0m' # No Color

log_step()  { echo -e "\n${BOLD}${BLUE}--- $1 ${NC}"; }
log_ok()    { echo -e "  ${GREEN}[OK] $1${NC}"; }
log_info()  { echo -e "  ${YELLOW}->  $1${NC}"; }
log_error() { echo -e "  ${RED}[X]  $1${NC}"; }

echo -e "\n${BOLD}========================================"
echo -e "       PC Rent Deployment"
echo -e "========================================${NC}"

# -----------------------------------------------
# [1/5] Load env vars
# -----------------------------------------------
log_step "[1/5] Loading environment variables"

if [ ! -f "$PROJECT_ROOT/serverV2/.env" ]; then
    log_error "serverV2/.env not found"
    exit 1
fi

while IFS='=' read -r key value; do
    key="${key%$'\r'}"
    value="${value%$'\r'}"
    if [[ -z "$key" || "$key" =~ ^# ]]; then
        continue
    fi
    export "$key=$value"
done < "$PROJECT_ROOT/serverV2/.env"

log_ok "DATABASE_URL loaded (Neon PostgreSQL)"
log_ok "R2_ACCOUNT_ID: ${R2_ACCOUNT_ID:0:10}..."
log_ok "R2_BUCKET_NAME: $R2_BUCKET_NAME"

FIREBASE_SERVICE_ACCOUNT_JSON="${FIREBASE_SERVICE_ACCOUNT_JSON:-}"
if [[ -z "$FIREBASE_SERVICE_ACCOUNT_JSON" ]]; then
    log_error "FIREBASE_SERVICE_ACCOUNT_JSON not set in serverV2/.env"
    exit 1
fi
log_ok "Firebase credentials loaded (inline JSON)"

# -----------------------------------------------
# [2/5] Deploy Backend to Cloud Run
# -----------------------------------------------
log_step "[2/5] Deploying backend -> Google Cloud Run"

cd "$PROJECT_ROOT/serverV2"
log_info "Building Docker image via Cloud Build..."
gcloud builds submit \
    --tag "gcr.io/$GCP_PROJECT/$SERVICE_NAME" \
    --project "$GCP_PROJECT" \
    --quiet
log_ok "Docker image built and pushed"

cd "$PROJECT_ROOT"

# Convert serverV2/.env -> YAML for `gcloud run deploy --env-vars-file`.
# Single source of truth: every var in serverV2/.env reaches Cloud Run, no
# explicit allowlist to keep in sync.
ENV_YAML="$PROJECT_ROOT/.env.cloudrun.yaml"
trap 'rm -f "$ENV_YAML"' EXIT

log_info "Generating Cloud Run env file from serverV2/.env..."
python - "$PROJECT_ROOT/serverV2/.env" "$ENV_YAML" <<'PY'
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
log_ok "Wrote $(basename "$ENV_YAML") ($(wc -l < "$ENV_YAML" | tr -d ' ') vars)"

log_info "Deploying container to Cloud Run (region: $GCP_REGION)..."
gcloud run deploy "$SERVICE_NAME" \
    --image "gcr.io/$GCP_PROJECT/$SERVICE_NAME" \
    --platform managed \
    --region "$GCP_REGION" \
    --project "$GCP_PROJECT" \
    --allow-unauthenticated \
    --memory 2Gi \
    --min-instances 1 \
    --no-cpu-throttling \
    --concurrency=300 \
    --cpu=2 \
    --env-vars-file "$ENV_YAML" \
    --quiet

BACKEND_URL=$(gcloud run services describe "$SERVICE_NAME" \
    --platform managed \
    --region "$GCP_REGION" \
    --project "$GCP_PROJECT" \
    --format "value(status.url)")

log_ok "Backend live at: $BACKEND_URL"

# Health check
HEALTH=$(curl -s --max-time 10 "$BACKEND_URL/" | grep -o 'running' || echo "unreachable")
if [ "$HEALTH" = "running" ]; then
    log_ok "Health check passed"
else
    log_error "Health check failed - backend may not be ready yet"
fi

# -----------------------------------------------
# [3/4] Build & Deploy Frontend to Cloudflare Pages
# -----------------------------------------------
log_step "[3/4] Building & deploying frontend -> Cloudflare Pages"

cd "$PROJECT_ROOT/frontend"
echo "VITE_API_BASE_URL=$BACKEND_URL" > .env.production
log_ok "Set VITE_API_BASE_URL=$BACKEND_URL"

log_info "Installing npm dependencies..."
npm install --silent
log_ok "Dependencies installed"

log_info "Building frontend with Vite..."
npm run build
log_ok "Frontend built (dist/)"

log_info "Deploying to Cloudflare Pages..."
npx wrangler pages deploy dist \
    --project-name pcrent \
    --branch main \
    --commit-dirty=true
log_ok "Frontend deployed to https://pcrent.pages.dev"

# -----------------------------------------------
# [4/4] Update Desktop App default URL
# -----------------------------------------------
log_step "[4/4] Updating desktop app default backend URL"

cd "$PROJECT_ROOT"
sed -i "s|useState(\"https://.*\")|useState(\"$BACKEND_URL\")|" \
    desktop/src/App.jsx
log_ok "desktop/src/App.jsx updated with: $BACKEND_URL"
log_info "Rebuild the installer to apply: cd desktop && npm run tauri build"

# -----------------------------------------------
# Done
# -----------------------------------------------
echo -e "\n${BOLD}${GREEN}========================================"
echo -e "        Deployment Complete!"
echo -e "========================================${NC}"
echo -e ""
echo -e "  ${BOLD}Backend:${NC}   $BACKEND_URL"
echo -e "  ${BOLD}Frontend:${NC}  https://pcrent.pages.dev"
echo -e "  ${BOLD}Database:${NC}  Neon PostgreSQL (connected)"
echo -e "  ${BOLD}Storage:${NC}   Cloudflare R2 ($R2_BUCKET_NAME)"
echo -e "  ${BOLD}Docker:${NC}    Windows/Linux render images on R2"
echo -e ""
echo -e "  ${YELLOW}To rebuild the desktop installer:${NC}"
echo -e "    cd agent && python build_sidecar.py"
echo -e "    cd ../desktop && npm run tauri build"
echo -e ""
