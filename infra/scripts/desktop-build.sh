#!/bin/bash
# ============================================================
# build-desktop.sh ENV
# ------------------------------------------------------------
# Builds a per-env Forge desktop installer.  Temp-copies
# desktop/ to infra/.build/desktop-$ENV/, swaps in the env's
# desktop.config.json values, runs the existing Tauri build,
# copies the NSIS installer to infra/dist/, then cleans up.
#
# desktop/ source tree is read-only -- this script never edits
# files in place.
#
# Patched files (in the temp tree only):
#   src/firebase/config.js          -- swap firebaseConfig literal
#   src/App.jsx                     -- swap hardcoded backendUrl
#   src/contexts/UserProfileContext.jsx (prod only)
#                                    -- strip localStorage override
#   src-tauri/tauri.conf.json       -- swap productName + identifier
#
# Pre-reqs:
#   * infra/envs/$ENV/desktop.config.json filled in
#   * npm + cargo + Tauri prereqs installed (same as legacy build)
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

echo "==> Patching $BUILD_DIR with $ENV config"
python - "$CONFIG_FILE" "$BUILD_DIR" "$ENV" <<'PY'
import json, re, sys, pathlib
config_file = pathlib.Path(sys.argv[1])
build_dir = pathlib.Path(sys.argv[2])
env = sys.argv[3]

with config_file.open() as f:
    cfg = json.load(f)

backend_url = cfg["backendUrl"]
firebase = cfg["firebaseConfig"]
product_name = cfg["productName"]
identifier = cfg["identifier"]
allow_override = cfg.get("allowLocalStorageBackendOverride", True)

if not backend_url:
    raise SystemExit(f"ERROR: {config_file} -> 'backendUrl' must be set")
required_fb = ("apiKey", "authDomain", "projectId", "storageBucket", "messagingSenderId", "appId")
missing = [k for k in required_fb if not firebase.get(k)]
if missing:
    raise SystemExit(f"ERROR: {config_file} -> firebaseConfig missing: {missing}")

# --- src/firebase/config.js: replace the firebaseConfig literal ---
cfg_js = build_dir / "src" / "firebase" / "config.js"
text = cfg_js.read_text()
new_literal = "const firebaseConfig = " + json.dumps(firebase, indent=2) + ";"
patched = re.sub(
    r"const firebaseConfig = \{[\s\S]*?\};",
    new_literal,
    text,
    count=1,
)
if patched == text:
    raise SystemExit(f"ERROR: failed to locate firebaseConfig literal in {cfg_js}")
cfg_js.write_text(patched)
print(f"  [ok] {cfg_js.name} -- firebaseConfig swapped")

# --- src/App.jsx: replace hardcoded backendUrl literal ---
app_jsx = build_dir / "src" / "App.jsx"
text = app_jsx.read_text()
patched = re.sub(
    r'const backendUrl = "https://[^"]+";',
    f'const backendUrl = "{backend_url}";',
    text,
    count=1,
)
if patched == text:
    raise SystemExit(f"ERROR: failed to locate backendUrl literal in {app_jsx}")
app_jsx.write_text(patched)
print(f"  [ok] {app_jsx.name} -- backendUrl = {backend_url}")

# --- src/contexts/UserProfileContext.jsx (prod only): strip override ---
ctx = build_dir / "src" / "contexts" / "UserProfileContext.jsx"
if not allow_override:
    text = ctx.read_text()
    patched = re.sub(
        r'\s*localStorage\.getItem\("pcrent_backend_url"\)\s*\|\|\s*',
        " ",
        text,
        count=1,
    )
    if patched == text:
        raise SystemExit(f"ERROR: failed to locate localStorage override in {ctx}")
    ctx.write_text(patched)
    print(f"  [ok] {ctx.name} -- localStorage override removed (prod)")
else:
    print(f"  [skip] {ctx.name} -- override left in (allowLocalStorageBackendOverride=true)")

# --- src-tauri/tauri.conf.json: swap productName + identifier ---
tauri_conf = build_dir / "src-tauri" / "tauri.conf.json"
conf = json.loads(tauri_conf.read_text())
conf["productName"] = product_name
conf["identifier"] = identifier
tauri_conf.write_text(json.dumps(conf, indent=2) + "\n")
print(f"  [ok] tauri.conf.json -- productName={product_name!r}, identifier={identifier!r}")
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
