#!/bin/bash
# desktop-dev.sh ENV
# Runs `npm run tauri dev` pointed at ENV's backend WITHOUT editing source.
# Writes a gitignored desktop/.env.local with the env's Vite vars; Vite layers
# it over the committed desktop/.env defaults.  A leftover .env.local is
# harmless (gitignored, overwritten next run) -- no in-place patching, no
# .bak, no "previous run crashed" recovery wall.
#
# Note: the window title / app identifier stay at tauri.conf.json's committed
# default for dev sessions (cosmetic); only desktop-build.sh sets them per-env
# (installer identity).  The backend the app talks to IS set per-env here.

set -euo pipefail

if [ $# -lt 1 ]; then
    echo "usage: $(basename "$0") ENV" >&2
    exit 2
fi

ENV="$1"
if [[ ! "$ENV" =~ ^(dev|staging|prod)$ ]]; then
    echo "ENV must be dev | staging | prod (got '$ENV')" >&2
    exit 2
fi

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CONFIG_FILE="$PROJECT_ROOT/infra/envs/$ENV/desktop.config.json"
if [ ! -f "$CONFIG_FILE" ]; then
    echo "$CONFIG_FILE not found.  Copy .example and fill in first." >&2
    exit 1
fi

DESKTOP="$PROJECT_ROOT/desktop"
ENV_LOCAL="$DESKTOP/.env.local"

cleanup() { rm -f "$ENV_LOCAL"; }
trap cleanup EXIT

echo "==> Writing $ENV Vite config -> desktop/.env.local"
python - "$CONFIG_FILE" "$ENV_LOCAL" <<'PY'
import json, sys
cfg = json.load(open(sys.argv[1]))
fb = cfg["firebaseConfig"]
backend = cfg["backendUrl"]
allow = cfg.get("allowLocalStorageBackendOverride", True)

required = ("apiKey", "authDomain", "projectId", "storageBucket", "messagingSenderId", "appId")
missing = [k for k in required if not fb.get(k)]
if missing:
    raise SystemExit(f"firebaseConfig missing: {missing}")
if not backend:
    raise SystemExit("backendUrl missing")

google = cfg.get("googleOAuth", {})
google_missing = [k for k in ("clientId", "clientSecret") if not google.get(k)]
if google_missing:
    raise SystemExit(f"googleOAuth missing: {google_missing}")

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
open(sys.argv[2], "w").write("\n".join(lines) + "\n")
print(f"  backend={backend}")
PY

echo "==> Running tauri dev (Ctrl-C to stop; .env.local removed on exit)"
( cd "$DESKTOP" && npm run tauri dev )
