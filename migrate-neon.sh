#!/bin/bash
set -euo pipefail

# ============================================================
# PC Rent - Neon -> Neon database migration (serverV2 Postgres)
# ============================================================
# Dumps the OLD Neon database and restores it into the NEW one
# (schema + data) so the new DB has the legacy v1 tables that
# serverV2 only ALTERs -- jobs, render_groups, machines,
# dispatch_queue, user_input_files -- plus every row.  Without
# this, serverV2 boot crashes on the first `ALTER TABLE jobs`.
#
# This touches Postgres only.  Firestore (users, token balances,
# roles) and Redis are separate infra and are NOT migrated here.
#
# Usage (simplest -- reads both URLs from serverV2/.env):
#   bash migrate-neon.sh [--yes]
#     NEW db = the active        `DATABASE_URL=` line
#     OLD db = the first commented `# DATABASE_URL=` line
#
# Or pass them explicitly (these override .env):
#   MIGRATE_OLD_URL='postgresql://...' MIGRATE_NEW_URL='postgresql://...' \
#     bash migrate-neon.sh [--yes]
#
# Pooled (`-pooler`) hosts are auto-converted to the DIRECT endpoint
# for dump/restore (pgbouncer can choke on restore).  serverV2/.env is
# gitignored, so credentials never enter a tracked file.
#
# Run this during a quiet window: with --min-instances 1 the old DB
# is written to continuously, so dump while no renders are active
# (or stop the old service first) to get a consistent snapshot.

GREEN='\033[0;32m'; BLUE='\033[0;34m'; YELLOW='\033[1;33m'
RED='\033[0;31m'; BOLD='\033[1m'; NC='\033[0m'
log_step()  { echo -e "\n${BOLD}${BLUE}--- $1 ${NC}"; }
log_ok()    { echo -e "  ${GREEN}[OK] $1${NC}"; }
log_info()  { echo -e "  ${YELLOW}->  $1${NC}"; }
log_error() { echo -e "  ${RED}[X]  $1${NC}"; }

ASSUME_YES=0
[[ "${1:-}" == "--yes" || "${1:-}" == "-y" ]] && ASSUME_YES=1

# -----------------------------------------------
# [1/5] Validate inputs + tooling
# -----------------------------------------------
log_step "[1/5] Resolving connection strings and tooling"

# Source of truth: explicit env vars win; otherwise read serverV2/.env --
# the active DATABASE_URL line is the NEW db, the first commented
# `# DATABASE_URL=` line is the OLD db.  .env is gitignored.
ENV_FILE="$(cd "$(dirname "$0")" && pwd)/serverV2/.env"
NEW_FROM_ENV=""; OLD_FROM_ENV=""
if [[ -f "$ENV_FILE" ]]; then
    NEW_FROM_ENV=$(grep -E '^[[:space:]]*DATABASE_URL=' "$ENV_FILE" | head -1 \
        | sed -E 's/^[[:space:]]*DATABASE_URL=//' | tr -d '\r')
    OLD_FROM_ENV=$(grep -E '^[[:space:]]*#[[:space:]]*DATABASE_URL=' "$ENV_FILE" | head -1 \
        | sed -E 's/^[[:space:]]*#[[:space:]]*DATABASE_URL=//' | tr -d '\r')
fi

OLD_URL="${MIGRATE_OLD_URL:-$OLD_FROM_ENV}"
NEW_URL="${MIGRATE_NEW_URL:-$NEW_FROM_ENV}"

if [[ -z "$OLD_URL" || -z "$NEW_URL" ]]; then
    log_error "No URLs found. Set MIGRATE_OLD_URL / MIGRATE_NEW_URL, or put an active"
    log_error "DATABASE_URL (new) and a commented '# DATABASE_URL' (old) in serverV2/.env."
    exit 1
fi
[[ -z "${MIGRATE_OLD_URL:-}" && -n "$OLD_FROM_ENV" ]] && log_info "OLD url read from serverV2/.env (commented DATABASE_URL)"
[[ -z "${MIGRATE_NEW_URL:-}" && -n "$NEW_FROM_ENV" ]] && log_info "NEW url read from serverV2/.env (active DATABASE_URL)"
if [[ "$OLD_URL" == "$NEW_URL" ]]; then
    log_error "OLD and NEW URLs are identical -- refusing to dump a DB onto itself."
    exit 1
fi

for cmd in pg_dump pg_restore psql; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
        log_error "'$cmd' not found on PATH. Install the PostgreSQL client tools."
        exit 1
    fi
done

# Neon refuses non-TLS connections -- ensure sslmode=require on both.
ensure_sslmode() {
    local url="$1"
    if [[ "$url" == *"sslmode="* ]]; then echo "$url"; return; fi
    if [[ "$url" == *"?"* ]]; then echo "${url}&sslmode=require"; else echo "${url}?sslmode=require"; fi
}
OLD_FIXED="$(ensure_sslmode "$OLD_URL")"
NEW_FIXED="$(ensure_sslmode "$NEW_URL")"
[[ "$OLD_FIXED" != "$OLD_URL" ]] && log_info "Appended sslmode=require to OLD URL"
[[ "$NEW_FIXED" != "$NEW_URL" ]] && log_info "Appended sslmode=require to NEW URL"
OLD_URL="$OLD_FIXED"; NEW_URL="$NEW_FIXED"

# Neon's pgbouncer pooler can choke on restore; use the direct endpoint
# for dump/restore by stripping `-pooler` from the host.
to_direct() { echo "$1" | sed -E 's/-pooler\./\./'; }
if [[ "$OLD_URL" == *"-pooler."* ]]; then OLD_URL="$(to_direct "$OLD_URL")"; log_info "OLD: converted pooled host -> direct endpoint"; fi
if [[ "$NEW_URL" == *"-pooler."* ]]; then NEW_URL="$(to_direct "$NEW_URL")"; log_info "NEW: converted pooled host -> direct endpoint"; fi
log_ok "Inputs look sane; pg_dump / pg_restore / psql present"

# -----------------------------------------------
# [2/5] Connectivity + version compatibility
# -----------------------------------------------
log_step "[2/5] Checking connectivity and Postgres versions"

check_conn() {  # $1=label $2=url
    if ! psql "$2" -tAc "SELECT 1" >/dev/null 2>&1; then
        log_error "Cannot connect to $1 database. Check the URL / credentials / network."
        exit 1
    fi
}
check_conn "OLD" "$OLD_URL"
check_conn "NEW" "$NEW_URL"
log_ok "Connected to both databases"

server_major() {  # $1=url -> major version int (e.g. 16)
    local num
    num=$(psql "$1" -tAc "SHOW server_version_num" 2>/dev/null | tr -d '[:space:]')
    echo $(( num / 10000 ))
}
CLIENT_MAJOR=$(pg_dump --version | grep -oE '[0-9]+' | head -1)
OLD_MAJOR=$(server_major "$OLD_URL")
NEW_MAJOR=$(server_major "$NEW_URL")
log_info "pg_dump client major: $CLIENT_MAJOR | OLD server: $OLD_MAJOR | NEW server: $NEW_MAJOR"

if (( CLIENT_MAJOR < OLD_MAJOR )) || (( CLIENT_MAJOR < NEW_MAJOR )); then
    log_error "pg_dump ($CLIENT_MAJOR) is older than a server. Upgrade client tools to >= $(( OLD_MAJOR > NEW_MAJOR ? OLD_MAJOR : NEW_MAJOR ))."
    exit 1
fi
log_ok "Client version is compatible with both servers"

# -----------------------------------------------
# [3/5] Dump OLD (schema + data)
# -----------------------------------------------
log_step "[3/5] Dumping OLD database"

DUMP_FILE="pcrent-neon-$(date +%Y%m%d-%H%M%S).dump"
log_info "Writing $DUMP_FILE (custom format, no owner/acl)..."
pg_dump "$OLD_URL" -Fc --no-owner --no-acl -f "$DUMP_FILE"
DUMP_SIZE=$(du -h "$DUMP_FILE" | cut -f1)
log_ok "Dump complete ($DUMP_SIZE)"

# -----------------------------------------------
# [4/5] Restore into NEW
# -----------------------------------------------
log_step "[4/5] Restoring into NEW database"

if (( ASSUME_YES == 0 )); then
    echo -en "  ${YELLOW}Restore $DUMP_FILE into the NEW database now? [y/N] ${NC}"
    read -r reply
    if [[ ! "$reply" =~ ^[Yy]$ ]]; then
        log_info "Aborted before restore. Dump kept at $DUMP_FILE"
        exit 0
    fi
fi

# pg_restore returns non-zero on benign warnings (e.g. role grants
# skipped by --no-owner). Capture the code and report rather than
# aborting the script outright.
set +e
pg_restore --no-owner --no-acl -d "$NEW_URL" "$DUMP_FILE"
RESTORE_RC=$?
set -e
if (( RESTORE_RC != 0 )); then
    log_info "pg_restore exited $RESTORE_RC (often benign with --no-owner). Review output above."
else
    log_ok "Restore completed cleanly"
fi

# -----------------------------------------------
# [5/5] Verify
# -----------------------------------------------
log_step "[5/5] Verifying the new database"

TABLE_COUNT=$(psql "$NEW_URL" -tAc \
    "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'" | tr -d '[:space:]')
log_info "public tables in NEW db: $TABLE_COUNT"
for t in jobs render_groups machines dispatch_queue user_input_files; do
    if psql "$NEW_URL" -tAc "SELECT to_regclass('public.$t')" | grep -q "$t"; then
        n=$(psql "$NEW_URL" -tAc "SELECT count(*) FROM public.$t" | tr -d '[:space:]')
        log_ok "$t present ($n rows)"
    else
        log_error "$t MISSING -- serverV2 boot will fail. Investigate the restore."
    fi
done

echo -e "\n${BOLD}${GREEN}========================================"
echo -e "        Neon migration done"
echo -e "========================================${NC}"
echo -e ""
echo -e "  ${BOLD}Next:${NC}"
echo -e "    1. Set DATABASE_URL in serverV2/.env to the new ${BOLD}POOLED${NC} (-pooler) URL"
echo -e "       (keep ?sslmode=require)."
echo -e "    2. Run ./deploy.sh -- it propagates .env to Cloud Run and health-checks."
echo -e "    3. Dump kept at: $DUMP_FILE"
echo -e ""
