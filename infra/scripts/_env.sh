#!/bin/bash
# Shared env-loading helper.  Every script under infra/scripts/ sources
# this with ``source "$(dirname "$0")/_env.sh"``.  Validates the ENV
# arg and exports every line of infra/envs/$ENV/.env so subsequent
# commands see the variables.
#
# Usage from a wrapping script:
#   source "$(dirname "$0")/_env.sh" "$1"   # $1 is dev / staging / prod
#
# After sourcing, $ENV is set and every var in the env file is exported.

set -euo pipefail

if [ $# -lt 1 ]; then
    echo "ERROR: usage: $(basename "${BASH_SOURCE[1]}") ENV [args...]" >&2
    echo "  ENV = dev | staging | prod" >&2
    exit 2
fi

ENV="$1"
if [[ ! "$ENV" =~ ^(dev|staging|prod)$ ]]; then
    echo "ERROR: ENV must be one of: dev, staging, prod (got '$ENV')" >&2
    exit 2
fi
export ENV

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export PROJECT_ROOT

ENV_FILE="$PROJECT_ROOT/infra/envs/$ENV/.env"
if [ ! -f "$ENV_FILE" ]; then
    echo "ERROR: $ENV_FILE not found." >&2
    echo "       Copy infra/envs/$ENV/.env.example to infra/envs/$ENV/.env" >&2
    echo "       and fill in the values, then re-run." >&2
    exit 1
fi
export ENV_FILE

# Flatten multi-line JSON values (e.g. FIREBASE_SERVICE_ACCOUNT_JSON
# pasted pretty-printed) into single-line compact JSON BEFORE sourcing.
# Bash source-ing would otherwise treat each JSON line as a command.
# Python is the only dependency; available on every machine that runs
# the other scripts (gcloud / terraform / etc.).
ENV_FILE_FLAT="$(mktemp)"
export ENV_FILE_FLAT  # so subprocess (python in deploy-*.sh) can read it
trap 'rm -f "$ENV_FILE_FLAT"' EXIT

python - "$ENV_FILE" "$ENV_FILE_FLAT" <<'PY'
import json, sys
src, dst = sys.argv[1], sys.argv[2]
with open(src, encoding="utf-8") as f:
    lines = f.read().splitlines()

out = []
i = 0
while i < len(lines):
    line = lines[i]
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in line:
        out.append(line)
        i += 1
        continue
    k, _, v = line.partition("=")
    # Multi-line JSON: value starts with `{` and braces not yet balanced.
    if v.lstrip().startswith("{") and v.count("{") > v.count("}"):
        collected = [v]
        depth = v.count("{") - v.count("}")
        i += 1
        while depth > 0 and i < len(lines):
            collected.append(lines[i])
            joined_so_far = "".join(collected)
            depth = joined_so_far.count("{") - joined_so_far.count("}")
            i += 1
        joined = "".join(collected).strip()
        try:
            obj = json.loads(joined)
            v = json.dumps(obj, separators=(",", ":"))
        except Exception:
            # Fallback: just strip line breaks
            v = joined.replace("\n", "").replace("\r", "")
        out.append(f"{k}={v}")
    else:
        out.append(line)
        i += 1

with open(dst, "w", encoding="utf-8") as f:
    f.write("\n".join(out))
    f.write("\n")  # trailing newline so `while read` sees the last line
PY

# Export every assignment in the flattened file using while-read
# instead of `source` -- shell sourcing strips `"` chars inside an
# unquoted value (so {"type":"foo"} becomes {type:foo}, invalid JSON).
# This pattern matches the existing root deploy.sh.
while IFS='=' read -r key value; do
    key="${key%$'\r'}"
    value="${value%$'\r'}"
    [[ -z "$key" || "$key" =~ ^# ]] && continue
    export "$key=$value"
done < "$ENV_FILE_FLAT"
