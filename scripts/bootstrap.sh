#!/usr/bin/env bash
#
# Reproducible local setup for Flock OS / Frappe.
#
# What it does (all idempotent — safe to re-run):
#   0. Loads ./../.env (secrets stay local, never committed).
#   1. Checks prerequisites (Python 3.12, bench, uv, node, Redis, MariaDB).
#   2. Preps MariaDB for TCP auth (delegates to bootstrap-db.sh).
#   3. `bench init` the runtime bench (at $BENCH_DIR) with Frappe v15 if missing.
#   4. Installs the flock_os app from this repo via `bench get-app`.
#   5. Creates the Frappe site ($SITE_NAME) with flock_os installed.
#   6. Optionally builds front-end assets (skip with SKIP_ASSETS=1).
#
# The bench runtime lives OUTSIDE the repo (at $BENCH_DIR) so the tracked tree
# holds only app source + CI + scripts. The flock_os app is installed as an
# editable (pip -e) package from this repo, so edits here are live in the site.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

ENV_FILE="$REPO_ROOT/.env"
if [ ! -f "$ENV_FILE" ]; then
  echo "ERROR: $ENV_FILE not found. Run: cp .env.example .env  (and fill it in)." >&2
  exit 1
fi
# shellcheck disable=SC1090
set -a; . "$ENV_FILE"; set +a

: "${MARIADB_ROOT_PASSWORD:?MARIADB_ROOT_PASSWORD must be set in .env}"
: "${BENCH_DIR:?BENCH_DIR must be set in .env}"
: "${SITE_NAME:?SITE_NAME must be set in .env}"
SITE_ADMIN_PASSWORD="${SITE_ADMIN_PASSWORD:-}"
PYTHON_BIN="${PYTHON_BIN:-/usr/local/bin/python3.12}"
FRAPPE_BRANCH="${FRAPPE_BRANCH:-version-15}"

echo "==> [0/6] Configuration"
echo "    repo:     $REPO_ROOT"
echo "    bench:    $BENCH_DIR"
echo "    site:     $SITE_NAME"
echo "    python:   $PYTHON_BIN"
echo "    frappe:   $FRAPPE_BRANCH"

echo "==> [1/6] Checking prerequisites"
need() { command -v "$1" >/dev/null 2>&1 || { echo "ERROR: '$1' not found on PATH." >&2; exit 1; }; }
need bench; need uv; need node; need npm; need redis-cli; need mysql; need git
"$PYTHON_BIN" --version
echo "    prerequisites OK"

echo "==> [2/6] Preparing MariaDB for TCP auth (frappe_root)"
bash "$SCRIPT_DIR/bootstrap-db.sh"

echo "==> [3/6] Initializing bench (if missing)"
# --no-backups avoids the end-of-init crontab setup which can stall on macOS;
# backups are handled by a separate scheduled runbook instead.
BENCH_FLAGS=(--no-backups)
if [ "${SKIP_ASSETS:-0}" = "1" ]; then BENCH_FLAGS+=(--skip-assets); fi
if [ ! -d "$BENCH_DIR/env" ]; then
  # bench init refuses a non-empty dir; ensure a clean target.
  rm -rf "$BENCH_DIR"
  bench init "$BENCH_DIR" --python "$PYTHON_BIN" --frappe-branch "$FRAPPE_BRANCH" "${BENCH_FLAGS[@]}"
else
  echo "    bench already initialized at $BENCH_DIR — skipping."
fi

echo "==> [4/6] Installing flock_os app from $REPO_ROOT"
if [ ! -e "$BENCH_DIR/apps/flock_os" ]; then
  (cd "$BENCH_DIR" && command bench get-app "$REPO_ROOT")
else
  echo "    flock_os already linked at $BENCH_DIR/apps/flock_os — updating."
  (cd "$BENCH_DIR" && command bench update --pull --patch --no-backup) || true
fi

echo "==> [5/6] Creating site $SITE_NAME (with flock_os) if missing"
NEW_SITE_ARGS=(--db-root-username frappe_root --db-root-password "$MARIADB_ROOT_PASSWORD")
if [ -n "$SITE_ADMIN_PASSWORD" ]; then
  NEW_SITE_ARGS+=(--admin-password "$SITE_ADMIN_PASSWORD")
fi
if [ ! -d "$BENCH_DIR/sites/$SITE_NAME" ]; then
  (cd "$BENCH_DIR" && command bench new-site "$SITE_NAME" "${NEW_SITE_ARGS[@]}" --install-app flock_os)
  (cd "$BENCH_DIR" && command bench --site "$SITE_NAME" use "$SITE_NAME")
else
  echo "    site $SITE_NAME already exists — skipping."
fi

echo "==> [6/6] Building front-end assets"
if [ "${SKIP_ASSETS:-0}" = "1" ]; then
  echo "    SKIP_ASSETS=1 — skipping. Run 'bench build' later when needed."
else
  (cd "$BENCH_DIR" && command bench build) || echo "    WARN: bench build had errors (assets). Site still usable for API/test work." >&2
fi

echo
echo "==> Done. Next:"
echo "    cd $BENCH_DIR"
echo "    bench --site $SITE_NAME serve   # start dev server on :8000"
echo "    bench --site $SITE_NAME execute 'flock_os.utils.bench_helper.version'  # sanity"
echo "    bench --site $SITE_NAME run-tests --app flock_os   # Frappe integration tests"
