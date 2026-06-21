#!/bin/bash
# migrate-db.sh ENV
# Copies the SCHEMA (no data) from the test Neon DB into the env's
# Neon DB so serverV2's init_db() can ALTER its expected v1 tables.
# Idempotent (uses CREATE ... IF NOT EXISTS via pg_dump's --clean=no).
# Required on first cold-start for any new env (tables don't exist yet);
# re-runnable after legacy schema changes.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_env.sh
source "$SCRIPT_DIR/_env.sh" "$1"

: "${DATABASE_URL:?DATABASE_URL must be set in $ENV_FILE}"

# Source = test env, read directly from serverV2/.env (single-line).
SRC_DB="$(grep '^DATABASE_URL=' "$PROJECT_ROOT/serverV2/.env" | head -1 | sed 's/^DATABASE_URL=//' | tr -d '\r')"
if [ -z "$SRC_DB" ]; then
    echo "DATABASE_URL not found in serverV2/.env" >&2
    exit 1
fi
DST_DB="$DATABASE_URL"

if [ "$SRC_DB" = "$DST_DB" ]; then
    echo "ERROR: source and destination URLs are identical -- would dump test onto itself" >&2
    exit 1
fi

# Strip ``-pooler`` from both: pg_dump / pg_restore choke on pgbouncer.
to_direct() { echo "$1" | sed -E 's/-pooler\././'; }
SRC_DB="$(to_direct "$SRC_DB")"
DST_DB="$(to_direct "$DST_DB")"

for cmd in pg_dump psql; do
    command -v "$cmd" >/dev/null 2>&1 || { echo "$cmd not on PATH"; exit 1; }
done

echo "==> Copying schema (no data) from test -> $ENV"

# --schema-only: DDL only, no INSERTs.  --no-owner / --no-acl: skip
# role grants Neon doesn't allow.  Piped straight into the dest.
pg_dump --schema-only --no-owner --no-acl "$SRC_DB" | psql -v ON_ERROR_STOP=1 "$DST_DB"

echo ""
echo "Done.  $ENV schema is current.  Re-run deploy-server.sh next."
