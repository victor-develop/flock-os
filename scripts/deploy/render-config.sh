#!/usr/bin/env bash
#
# Render Flock OS site config from environment (FLO-246 Phase 6.1).
#
# The zero-secrets-in-repo gate. Reads deploy/templates/{site_config,common_site_config}.json.tmpl
# and renders them into <sites>/site_config.json and <sites>/common_site_config.json
# using envsubst. Secrets (DB_PASSWORD, SECRET_KEY, FLOCK_SIO_ADAPTER_REDIS, etc.)
# come from the secret manager (SOPS+age or cloud SM) — set as env on the host
# or injected by the deploy orchestrator. NOTHING is read from the repo.
#
# Required env (fail fast if any missing):
#   DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD,
#   REDIS_CACHE_URI, REDIS_QUEUE_URI, REDIS_SOCKETIO_URI,
#   FLOCK_SIO_ADAPTER_REDIS, SECRET_KEY, SITE_URL, FLOCK_ENV
#
# Optional env (have sensible defaults):
#   FLOCK_SIO_PROCESSES (default 4), MUTE_EMAILS (default 1),
#   DROPBOX_*, GDRIVE_* (empty = disabled)
#
# Usage:
#   scripts/deploy/render-config.sh [--sites-dir DIR] [--site NAME]
#                                   [--check] [--print-env]
#   scripts/deploy/render-config.sh --help
#
# --check: validate required env WITHOUT writing anything. Use in CI / pre-deploy
#          gates to fail loudly on a missing secret before the rolling deploy.
# --print-env: print the resolved config to stdout (with secrets REDACTED) for
#          runbook triage. Never logs raw secrets.
#
# Runbook: docs/development/deploy-runbook.md -> "Render site config".
set -euo pipefail

PROG="$(basename "${BASH_SOURCE[0]}")"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TEMPLATES_DIR="$REPO_ROOT/deploy/templates"

SITES_DIR=""
SITE_NAME="flock_os"
CHECK_ONLY=0
PRINT_ENV=0

usage() { sed -n '2,/^$/p' "${BASH_SOURCE[0]}" >&2; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --sites-dir) SITES_DIR="$2"; shift 2 ;;
        --sites-dir=*) SITES_DIR="${1#--sites-dir=}"; shift ;;
        --site) SITE_NAME="$2"; shift 2 ;;
        --site=*) SITE_NAME="${1#--site=}"; shift ;;
        --check) CHECK_ONLY=1; shift ;;
        --print-env) PRINT_ENV=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "$PROG: unknown arg: $1" >&2; exit 2 ;;
    esac
done

# Defaults that match the supervisor + Dockerfile.
: "${DB_PORT:=3306}"
: "${FLOCK_SIO_PROCESSES:=4}"
: "${MUTE_EMAILS:=1}"
: "${FLOCK_ENV:=prod}"

# Required env. Listing them explicitly = a fail-fast secret gate: a missing
# required var aborts the deploy loudly instead of letting the bench boot with
# a half-rendered config (the kind of silent regression that loses a real event).
REQUIRED_VARS=(
    DB_HOST DB_NAME DB_USER DB_PASSWORD
    REDIS_CACHE_URI REDIS_QUEUE_URI REDIS_SOCKETIO_URI
    FLOCK_SIO_ADAPTER_REDIS SECRET_KEY SITE_URL FLOCK_ENV
)
missing=()
for v in "${REQUIRED_VARS[@]}"; do
    if [[ -z "${!v:-}" ]]; then missing+=("$v"); fi
done
if (( ${#missing[@]} > 0 )); then
    echo "$PROG: missing required env vars: ${missing[*]}" >&2
    echo "$PROG: set them from the secret manager (SOPS+age / cloud SM) — see .env.example" >&2
    echo "$PROG: and docs/development/deploy-runbook.md -> Render site config." >&2
    exit 1
fi

if [[ "$PRINT_ENV" -eq 1 ]]; then
    # Redacted view for runbook triage. Never prints raw secret values.
    redact() {
        local v="$1"
        if [[ -z "${!v:-}" ]]; then printf '%s=(unset)\n' "$v"; return; fi
        printf '%s=<set,%d chars>\n' "$v" "${#v}"
    }
    echo "# render-config resolved env (redacted):"
    for v in "${REQUIRED_VARS[@]}" DB_PORT FLOCK_SIO_PROCESSES MUTE_EMAILS; do redact "$v"; done
    exit 0
fi

if [[ "$CHECK_ONLY" -eq 1 ]]; then
    echo "$PROG: --check OK (all required env present, redacted):"
    for v in "${REQUIRED_VARS[@]}"; do
        if [[ -n "${!v:-}" ]]; then echo "  $v=<set>"; else echo "  $v=(unset)"; fi
    done
    exit 0
fi

# Where to write. Default: the in-image sites dir; override for off-image renders.
if [[ -z "$SITES_DIR" ]]; then
    SITES_DIR="${BENCH_DIR:-/home/frappe/frappe-bench}/sites"
fi
mkdir -p "$SITES_DIR/$SITE_NAME"

command -v envsubst >/dev/null 2>&1 || {
    echo "$PROG: envsubst not found (apt-get install gettext-base / brew install gettext)." >&2
    exit 1
}

# envsubst resolves every ${VAR} in the template from the current environment.
# Keep the template schema in sync with deploy/templates/*.json.tmpl.
render() {
    local tmpl="$1" out="$2"
    [[ -f "$tmpl" ]] || { echo "$PROG: template missing: $tmpl" >&2; exit 1; }
    # The _comment field is documentation-only; strip it from the rendered file
    # so the deployed config is lean (and never carries a stray template hint).
    envsubst < "$tmpl" | jq 'del(._comment)' > "$out"
    chmod 0600 "$out"
    echo "$PROG: rendered $out"
}

render "$TEMPLATES_DIR/common_site_config.json.tmpl" "$SITES_DIR/common_site_config.json"
render "$TEMPLATES_DIR/site_config.json.tmpl"      "$SITES_DIR/$SITE_NAME/site_config.json"

echo "$PROG: site config rendered for site '$SITE_NAME' (sites dir: $SITES_DIR)."
echo "$PROG: secrets came from env (secret manager) — none were read from the repo."
