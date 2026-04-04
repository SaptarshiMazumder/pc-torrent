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
SERVICE_NAME="pcrent-server"

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

if [ ! -f "$PROJECT_ROOT/server/.env" ]; then
    log_error "server/.env not found. Create it from server/.env.example"
    exit 1
fi

while IFS='=' read -r key value; do
    key="${key%$'\r'}"
    value="${value%$'\r'}"
    if [[ -z "$key" || "$key" =~ ^# ]]; then
        continue
    fi
    export "$key=$value"
done < "$PROJECT_ROOT/server/.env"

log_ok "DATABASE_URL loaded (Neon PostgreSQL)"
log_ok "R2_ACCOUNT_ID: ${R2_ACCOUNT_ID:0:10}..."
log_ok "R2_BUCKET_NAME: $R2_BUCKET_NAME"
log_ok "CLOUDFLARE_API_TOKEN: ${CLOUDFLARE_API_TOKEN:0:8}..."

# -----------------------------------------------
# [2/5] Deploy Backend to Cloud Run
# -----------------------------------------------
log_step "[2/5] Deploying backend -> Google Cloud Run"

BUILD_DIR=$(mktemp -d)
cp "$PROJECT_ROOT/server/Dockerfile" "$BUILD_DIR/"
cp "$PROJECT_ROOT/server/requirements.txt" "$BUILD_DIR/"
cp "$PROJECT_ROOT/server/db.py" "$BUILD_DIR/"
cp "$PROJECT_ROOT/server/storage.py" "$BUILD_DIR/"
cp "$PROJECT_ROOT/server/main.py" "$BUILD_DIR/"
cp "$PROJECT_ROOT/server/runpod_dispatch.py" "$BUILD_DIR/"
cp "$PROJECT_ROOT/server/blend_parser.py" "$BUILD_DIR/"
log_ok "Copied source files to build context"

cd "$BUILD_DIR"
log_info "Building Docker image via Cloud Build..."
gcloud builds submit \
    --tag "gcr.io/$GCP_PROJECT/$SERVICE_NAME" \
    --project "$GCP_PROJECT" \
    --quiet
log_ok "Docker image built and pushed"

cd "$PROJECT_ROOT"
rm -rf "$BUILD_DIR" || true

log_info "Deploying container to Cloud Run (region: $GCP_REGION)..."
gcloud run deploy "$SERVICE_NAME" \
    --image "gcr.io/$GCP_PROJECT/$SERVICE_NAME" \
    --platform managed \
    --region "$GCP_REGION" \
    --project "$GCP_PROJECT" \
    --allow-unauthenticated \
    --memory 2Gi \
    --set-env-vars "DATABASE_URL=$DATABASE_URL" \
    --set-env-vars "R2_ACCOUNT_ID=$R2_ACCOUNT_ID" \
    --set-env-vars "R2_ACCESS_KEY_ID=$R2_ACCESS_KEY_ID" \
    --set-env-vars "R2_SECRET_ACCESS_KEY=$R2_SECRET_ACCESS_KEY" \
    --set-env-vars "R2_BUCKET_NAME=$R2_BUCKET_NAME" \
    --set-env-vars "RUNPOD_API_KEY=$RUNPOD_API_KEY" \
    --set-env-vars "RUNPOD_ENDPOINT_ID=${RUNPOD_ENDPOINT_ID:-}" \
    --set-env-vars "^@^RUNPOD_ENDPOINTS=${RUNPOD_ENDPOINTS:-}" \
    --set-env-vars "PUBLIC_BACKEND_URL=$PUBLIC_BACKEND_URL" \
    --set-env-vars "RUNPOD_GPU_MODEL=${RUNPOD_GPU_MODEL:-RunPod Serverless}" \
    --set-env-vars "RUNPOD_GPU_VRAM_GB=${RUNPOD_GPU_VRAM_GB:-24}" \
    --set-env-vars "RUNPOD_CPU_CORES=${RUNPOD_CPU_CORES:-16}" \
    --set-env-vars "RUNPOD_RAM_GB=${RUNPOD_RAM_GB:-64}" \
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
# [3/5] Sync Docker render images to R2
# -----------------------------------------------
log_step "[3/5] Syncing Windows/Linux Docker render images -> Cloudflare R2"

# Backward compatibility: UPLOAD_RENDER_IMAGE=1 means upload Windows image.
if [[ "${UPLOAD_RENDER_IMAGE:-}" == "1" ]] && [[ -z "${UPLOAD_RENDER_IMAGE_WINDOWS:-}" ]]; then
    export UPLOAD_RENDER_IMAGE_WINDOWS=1
fi

cd "$PROJECT_ROOT"

if [[ "${UPLOAD_RENDER_IMAGE_WINDOWS:-}" == "1" ]] && [ -f "$PROJECT_ROOT/server/docker/pcrent-render.tar.gz" ]; then
    IMAGE_SIZE=$(du -sh "$PROJECT_ROOT/server/docker/pcrent-render.tar.gz" | cut -f1)
    log_info "Uploading Windows image pcrent-render.tar.gz ($IMAGE_SIZE) to R2..."
    log_info "This may take a few minutes..."
    python upload_docker_image.py
    log_ok "Windows Docker render image synced to R2"
else
    log_info "Skipping Windows image upload (set UPLOAD_RENDER_IMAGE_WINDOWS=1)"
fi

if [[ "${UPLOAD_RENDER_IMAGE_LINUX:-}" == "1" ]] && [ -f "$PROJECT_ROOT/server/docker_linux/pcrent-render-linux.tar.gz" ]; then
    LINUX_IMAGE_SIZE=$(du -sh "$PROJECT_ROOT/server/docker_linux/pcrent-render-linux.tar.gz" | cut -f1)
    log_info "Uploading Linux image pcrent-render-linux.tar.gz ($LINUX_IMAGE_SIZE) to R2..."
    log_info "This may take a few minutes..."
    python upload_docker_image_linux.py
    log_ok "Linux Docker render image synced to R2"
else
    log_info "Skipping Linux image upload (set UPLOAD_RENDER_IMAGE_LINUX=1)"
fi

# -----------------------------------------------
# [4/5] Build & Deploy Frontend to Cloudflare Pages
# -----------------------------------------------
log_step "[4/5] Building & deploying frontend -> Cloudflare Pages"

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
# [5/5] Update Desktop App default URL
# -----------------------------------------------
log_step "[5/5] Updating desktop app default backend URL"

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
