#!/bin/bash
# ============================================================
# build-desktop.sh ENV
# ------------------------------------------------------------
# Builds a per-env Forge desktop installer.  Temp-copies
# desktop/ to infra/.build/desktop-$ENV/, writes the env's Vite
# vars into that copy's .env.local, sets the per-env installer
# identity in its tauri.conf.json, runs the Tauri build, copies
# the NSIS installer to infra/dist/, then cleans up.
#
# desktop/ source tree is read-only -- this script only touches
# the temp copy.  No source patching, no .bak.
#
# Per-env config (in the TEMP copy only):
#   .env.local                  -- VITE_* (backend URL, firebase, override)
#   src-tauri/tauri.conf.json   -- productName + identifier (installer id)
#
# Pre-reqs:
#   * infra/envs/$ENV/desktop.config.json filled in
#   * npm + cargo + Tauri prereqs installed
#   * Python 3 on PATH
#
# Usage:
#   bash infra/scripts/build-desktop.sh dev
# ============================================================

set -euo pipefail

if [ $# -lt 1 ]; then
    echo "ERROR: usage: $(basename "$0") ENV" >&2
    exit 2
fi

ENV="$1"
if [[ ! "$ENV" =~ ^(dev|staging|prod)$ ]]; then
    echo "ERROR: ENV must be dev, staging, or prod (got '$ENV')" >&2
    exit 2
fi

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CONFIG_FILE="$PROJECT_ROOT/infra/envs/$ENV/desktop.config.json"
if [ ! -f "$CONFIG_FILE" ]; then
    echo "ERROR: $CONFIG_FILE not found." >&2
    echo "       Copy infra/envs/$ENV/desktop.config.json.example to it and fill in." >&2
    exit 1
fi

BUILD_ROOT="$PROJECT_ROOT/infra/.build"
BUILD_DIR="$BUILD_ROOT/desktop-$ENV"
DIST_DIR="$PROJECT_ROOT/infra/dist"

mkdir -p "$BUILD_ROOT" "$DIST_DIR"

echo "==> Cleaning $BUILD_DIR"
rm -rf "$BUILD_DIR"

echo "==> Temp-copying desktop/ -> $BUILD_DIR"
# rsync would be cleaner, but Git Bash on Windows ships cp.  Excludes
# node_modules + cargo target so the copy is fast.
cp -r "$PROJECT_ROOT/desktop" "$BUILD_DIR"
rm -rf "$BUILD_DIR/node_modules" "$BUILD_DIR/src-tauri/target" "$BUILD_DIR/dist"

echo "==> Writing $ENV config into $BUILD_DIR"
python - "$CONFIG_FILE" "$BUILD_DIR" <<'PY'
import json, sys, pathlib
cfg = json.load(open(sys.argv[1]))
build_dir = pathlib.Path(sys.argv[2])

backend = cfg["backendUrl"]
fb = cfg["firebaseConfig"]
allow = cfg.get("allowLocalStorageBackendOverride", True)

if not backend:
    raise SystemExit("ERROR: backendUrl must be set")
required = ("apiKey", "authDomain", "projectId", "storageBucket", "messagingSenderId", "appId")
missing = [k for k in required if not fb.get(k)]
if missing:
    raise SystemExit(f"ERROR: firebaseConfig missing: {missing}")

google = cfg.get("googleOAuth", {})
google_missing = [k for k in ("clientId", "clientSecret") if not google.get(k)]
if google_missing:
    raise SystemExit(f"ERROR: googleOAuth missing: {google_missing}")

# Per-env Vite vars (override the committed desktop/.env defaults; .env.local
# beats .env in Vite's precedence).  Replaces the old JS source patching.
lines = [
    f"VITE_BACKEND_URL={backend}",
    f"VITE_FIREBASE_API_KEY={fb['apiKey']}",
    f"VITE_FIREBASE_AUTH_DOMAIN={fb['authDomain']}",
    f"VITE_FIREBASE_PROJECT_ID={fb['projectId']}",
    f"VITE_FIREBASE_STORAGE_BUCKET={fb['storageBucket']}",
    f"VITE_FIREBASE_MESSAGING_SENDER_ID={fb['messagingSenderId']}",
    f"VITE_FIREBASE_APP_ID={fb['appId']}",
    f"VITE_GOOGLE_OAUTH_CLIENT_ID={google['clientId']}",
    f"VITE_GOOGLE_OAUTH_CLIENT_SECRET={google['clientSecret']}",
    f"VITE_ALLOW_BACKEND_OVERRIDE={'true' if allow else 'false'}",
]
(build_dir / ".env.local").write_text("\n".join(lines) + "\n")
print(f"  [ok] .env.local -- backend={backend}, override={allow}")

# Per-env installer identity (window title + app id) so dev/staging/prod
# installers don't collide on a user's machine.  Patched in the TEMP copy
# only -- the real source tree is never touched.
tauri_conf = build_dir / "src-tauri" / "tauri.conf.json"
conf = json.loads(tauri_conf.read_text())
conf["productName"] = cfg["productName"]
conf["identifier"] = cfg["identifier"]
tauri_conf.write_text(json.dumps(conf, indent=2) + "\n")
print(f"  [ok] tauri.conf.json -- productName={cfg['productName']!r}, identifier={cfg['identifier']!r}")
PY

echo ""
echo "==> Building Python sidecar (agent/build_sidecar.py)"
( cd "$PROJECT_ROOT" && python agent/build_sidecar.py )

echo ""
echo "==> npm install + tauri build"
( cd "$BUILD_DIR" && npm install --silent && npm run tauri build )

echo ""
echo "==> Copying NSIS installer -> $DIST_DIR"
NSIS_OUT="$BUILD_DIR/src-tauri/target/release/bundle/nsis"
TARGET="$DIST_DIR/forge-${ENV}-setup.exe"
cp -f "$NSIS_OUT"/*-setup.exe "$TARGET"

echo ""
echo "==> Cleaning $BUILD_DIR"
rm -rf "$BUILD_DIR"

echo ""
echo "Done."
echo "  Installer: $TARGET"
