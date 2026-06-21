#!/bin/bash
# desktop-dev.sh ENV
# Patches desktop/ source IN-PLACE with the env's desktop.config.json,
# runs `npm run tauri dev`, then restores the original source on exit
# (clean or crash).  Hot reload works against the real source tree.

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
FILES=(
    "$DESKTOP/src/firebase/config.js"
    "$DESKTOP/src/App.jsx"
    "$DESKTOP/src-tauri/tauri.conf.json"
    "$DESKTOP/src/contexts/UserProfileContext.jsx"
)

# Refuse to start if any .bak file already exists (previous crash) --
# restore manually or delete .bak first.
for f in "${FILES[@]}"; do
    if [ -f "$f.bak" ]; then
        echo "ERROR: $f.bak exists.  Previous desktop-dev run crashed."
        echo "       Restore with: mv $f.bak $f"
        exit 1
    fi
done

# Back up + patch
for f in "${FILES[@]}"; do
    cp "$f" "$f.bak"
done

restore() {
    for f in "${FILES[@]}"; do
        [ -f "$f.bak" ] && mv -f "$f.bak" "$f"
    done
    echo "[restored desktop/ source]"
}
trap restore EXIT

echo "==> Patching desktop/ for $ENV"
python - "$CONFIG_FILE" "$DESKTOP" "$ENV" <<'PY'
import json, re, sys, pathlib
cfg = json.load(open(sys.argv[1]))
desktop = pathlib.Path(sys.argv[2])

backend = cfg["backendUrl"]
fb = cfg["firebaseConfig"]
product = cfg["productName"]
ident = cfg["identifier"]
allow_override = cfg.get("allowLocalStorageBackendOverride", True)

required = ("apiKey","authDomain","projectId","storageBucket","messagingSenderId","appId")
missing = [k for k in required if not fb.get(k)]
if missing:
    raise SystemExit(f"firebaseConfig missing: {missing}")
if not backend:
    raise SystemExit("backendUrl missing")

# firebase config
p = desktop / "src" / "firebase" / "config.js"
text = p.read_text()
new = "const firebaseConfig = " + json.dumps(fb, indent=2) + ";"
patched, n = re.subn(r"const firebaseConfig = \{[\s\S]*?\};", new, text, count=1)
if n == 0:
    raise SystemExit(f"failed to patch {p}: firebaseConfig literal not found")
p.write_text(patched)

# backendUrl
p = desktop / "src" / "App.jsx"
text = p.read_text()
patched, n = re.subn(r'const backendUrl = "https://[^"]+";', f'const backendUrl = "{backend}";', text, count=1)
if n == 0:
    raise SystemExit(f"failed to patch {p}: backendUrl literal not found")
p.write_text(patched)

# localStorage override (strip for prod)
p = desktop / "src" / "contexts" / "UserProfileContext.jsx"
if not allow_override:
    text = p.read_text()
    patched = re.sub(r'\s*localStorage\.getItem\("pcrent_backend_url"\)\s*\|\|\s*', " ", text, count=1)
    if patched == text:
        raise SystemExit(f"failed to patch {p}")
    p.write_text(patched)

# tauri.conf.json
p = desktop / "src-tauri" / "tauri.conf.json"
conf = json.loads(p.read_text())
conf["productName"] = product
conf["identifier"] = ident
p.write_text(json.dumps(conf, indent=2) + "\n")
print(f"  patched for env={sys.argv[3]}, backend={backend}")
PY

echo "==> Running tauri dev (Ctrl-C to stop; source will be restored)"
( cd "$DESKTOP" && npm run tauri dev )
