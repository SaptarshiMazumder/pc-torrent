#!/bin/bash
# ============================================================
# seed-firestore.sh ENV
# ------------------------------------------------------------
# Copies every document in the ``config`` collection from the
# TEST env's Firestore (creds read from serverV2/.env) into
# THIS env's Firestore (creds read from infra/envs/$ENV/.env).
#
# Per project rule: the bundled serverV2/config.json is just a
# seed default and is usually out-of-date.  Live Firestore in
# the test env is the source of truth -- that's what we copy.
#
# Idempotent.  Safe to re-run.  Refuses to run if source ==
# destination creds (would overwrite test config with itself).
#
# Collections copied: ``config/*`` only.  ``users/*`` and
# ``allocation_cost_file_formulas`` are intentionally NOT
# copied -- users sign in fresh per env; the formula cache
# rebuilds on demand.
#
# Usage:
#   bash infra/scripts/seed-firestore.sh dev
# ============================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_env.sh
source "$SCRIPT_DIR/_env.sh" "$1"

: "${FIREBASE_SERVICE_ACCOUNT_JSON:?FIREBASE_SERVICE_ACCOUNT_JSON must be set in $ENV_FILE (destination)}"

SOURCE_ENV_FILE="$PROJECT_ROOT/serverV2/.env"
if [ ! -f "$SOURCE_ENV_FILE" ]; then
    echo "ERROR: source env file $SOURCE_ENV_FILE not found" >&2
    exit 1
fi

echo "==> Copying Firestore config/* from test -> $ENV"

cd "$PROJECT_ROOT"
python - "$SOURCE_ENV_FILE" <<'PY'
import os, sys, json
import firebase_admin
from firebase_admin import credentials, firestore

src_env_file = sys.argv[1]

# Source creds: scan serverV2/.env for FIREBASE_SERVICE_ACCOUNT_JSON.
# That file is single-line; trivial grep is enough.
src_json = None
with open(src_env_file, encoding="utf-8") as f:
    for line in f:
        line = line.rstrip("\r\n")
        if line.startswith("FIREBASE_SERVICE_ACCOUNT_JSON="):
            src_json = line[len("FIREBASE_SERVICE_ACCOUNT_JSON="):]
            break
if not src_json:
    raise SystemExit(f"FIREBASE_SERVICE_ACCOUNT_JSON not found in {src_env_file}")

dst_json = os.environ["FIREBASE_SERVICE_ACCOUNT_JSON"]
if src_json.strip() == dst_json.strip():
    raise SystemExit(
        "ERROR: source and destination Firebase creds are IDENTICAL.\n"
        "       This would overwrite the test env's config with itself.\n"
        "       Check FIREBASE_SERVICE_ACCOUNT_JSON in the env's .env -- "
        "it should point at a DIFFERENT Firebase project than serverV2/.env."
    )

src_creds = json.loads(src_json)
dst_creds = json.loads(dst_json)
print(f"  source project: {src_creds['project_id']}")
print(f"  dest project:   {dst_creds['project_id']}")

src_app = firebase_admin.initialize_app(credentials.Certificate(src_creds), name="src")
dst_app = firebase_admin.initialize_app(credentials.Certificate(dst_creds), name="dst")
src_db = firestore.client(app=src_app)
dst_db = firestore.client(app=dst_app)

copied, skipped = [], []
for doc in src_db.collection("config").stream():
    data = doc.to_dict()
    if data is None:
        skipped.append(doc.id)
        continue
    dst_db.collection("config").document(doc.id).set(data)
    copied.append(doc.id)

if not copied:
    raise SystemExit("source has no config/* docs -- nothing to copy")

print(f"[ok] copied {len(copied)} config doc(s): {copied}")
if skipped:
    print(f"[warn] skipped empty docs: {skipped}")
PY

echo ""
echo "Done.  $ENV config/* is now mirrored from test."
