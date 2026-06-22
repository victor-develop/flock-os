#!/usr/bin/env bash
#
# Flock OS container entrypoint (FLO-246 Phase 6.1).
#
# Responsibilities (in order):
#   1. Render site_config.json + common_site_config.json from environment via
#      scripts/deploy/render-config.sh. Secrets come from env (set by the
#      secret manager — SOPS+age, cloud SM, or Frappe Cloud env injection). The
#      IMAGE never contains a secret; this is the zero-secrets-in-repo gate.
#   2. Wait for MariaDB + Redis to be reachable (they are sibling services,
#      not baked in — managed MariaDB + managed/dedicated Redis per ADR).
#   3. `bench migrate` — applies the flock_os versioned patches
#      (flock_os/patches.txt) idempotently. The self-healing redis-adapter
#      wiring (flock_os/utils/realtime_setup.py) re-runs on the after_migrate
#      hook, so the FLO-121 tier is armed even after a framework upgrade.
#   4. Exec the CMD (supervisord) which manages gunicorn + workers + scheduler
#      + the scaled-socketio tier + nginx.
#
# This is the unit-of-deploy on a Frappe Cloud Server plan VM (ADR target) and
# on any self-hosted VM (ADR fallback). Runbook: docs/development/deploy-runbook.md.
set -euo pipefail

log() { echo "[flock-os-entrypoint] $*"; }
err() { echo "[flock-os-entrypoint] $*" >&2; }

BENCH_DIR="${BENCH_DIR:-/home/frappe/frappe-bench}"
SITES_DIR="$BENCH_DIR/sites"
SITE_NAME="${SITE_NAME:-flock_os}"

cd "$BENCH_DIR"

# --- 1. Render config from env ------------------------------------------------
# Refuses to boot if a required secret is missing — fail fast at deploy, not at
# the first WS handshake. See scripts/deploy/render-config.sh for the schema.
log "rendering site config from environment"
if ! flock-os-render-config --sites-dir "$SITES_DIR" --site "$SITE_NAME"; then
    err "config render failed — refusing to boot with an incomplete config."
    err "required env: DB_HOST, DB_NAME, DB_USER, DB_PASSWORD, REDIS_CACHE_URI,"
    err "               REDIS_QUEUE_URI, REDIS_SOCKETIO_URI, FLOCK_SIO_ADAPTER_REDIS,"
    err "               SECRET_KEY. See .env.example + deploy-runbook.md."
    exit 1
fi

# --- 2. Wait for sibling services (MariaDB, Redis) ----------------------------
# They live outside the container (managed MariaDB + dedicated Redis per ADR).
# Wait up to ~60s for each; the deploy orchestration (Frappe Cloud / docker
# compose / k8s) is responsible for starting them in dependency order.
wait_for() {
    local label="$1" check_cmd="$2" i
    log "waiting for $label ..."
    for ((i = 0; i < 60; i++)); do
        if eval "$check_cmd" >/dev/null 2>&1; then
            log "$label is up"
            return 0
        fi
        sleep 1
    done
    err "$label did not become reachable in ~60s — check sibling services."
    return 1
}

DB_HOST="${DB_HOST:-127.0.0.1}"
DB_PORT="${DB_PORT:-3306}"
REDIS_CACHE_URI="${REDIS_CACHE_URI:-redis://127.0.0.1:13000}"

wait_for "MariaDB ($DB_HOST:$DB_PORT)" \
    "mariadb --connect-timeout=2 -h '$DB_HOST' -P '$DB_PORT' -u '$DB_USER' -p'$DB_PASSWORD' -e 'SELECT 1'"

# Redis check: parse host:port out of the cache URI (the dedicated adapter Redis
# is probed by scale-socketio.sh at tier start; here we just need the cache).
REDIS_HOST="$(printf '%s' "$REDIS_CACHE_URI" | sed -E 's#^redis://([^:/]+)(:[0-9]+)?/?.*#\1#')"
REDIS_PORT="$(printf '%s' "$REDIS_CACHE_URI" | sed -E 's#^redis://[^:/]+:([0-9]+)/?.*#\1#; t; s#.*#6379#')"
wait_for "Redis ($REDIS_HOST:$REDIS_PORT)" \
    "redis-cli -h '$REDIS_HOST' -p '$REDIS_PORT' ping"

# --- 3. Migrate ---------------------------------------------------------------
# Idempotent: applies pending flock_os patches (flock_os/patches.txt). The
# after_migrate hook (flock_os/utils/realtime_setup.py) re-arms the FLO-121
# redis-adapter wiring into the vendored realtime index.js — so even a
# `bench update` that rewrites index.js leaves the scaled tier functional.
log "running bench migrate"
bench --site "$SITE_NAME" migrate || {
    err "bench migrate failed — see logs above. Aborting boot."
    exit 1
}

# --- 4. Generate nginx socketio upstream from FLOCK_SIO_PROCESSES -------------
# The prod nginx (deploy/nginx/prod.conf) includes /etc/nginx/flock-os/socketio_upstream.conf
# for its sticky-L7 upstream. Generate it here so the upstream always matches the
# live backend count (ports 9001..900N). This replaces the old hardcoded 4-backend
# list that couldn't scale (architect fix FLO-246).
SIO_PROCESSES="${FLOCK_SIO_PROCESSES:-4}"
SIO_BASE_PORT="${FLOCK_SIO_BASE_PORT:-9001}"
UPSTREAM_FILE="/etc/nginx/flock-os/socketio_upstream.conf"
log "generating nginx socketio upstream ($SIO_PROCESSES backends from :$SIO_BASE_PORT)"
mkdir -p /etc/nginx/flock-os
{
    echo "ip_hash;"
    for ((i = 0; i < SIO_PROCESSES; i++)); do
        echo "server 127.0.0.1:$((SIO_BASE_PORT + i)) max_fails=3 fail_timeout=10s;"
    done
} > "$UPSTREAM_FILE"
log "wrote $UPSTREAM_FILE ($SIO_PROCESSES backends)"

# --- 5. Clear stale bench proc state, then exec the supervisor ----------------
# `bench start` writes a Procfile-based PID set; a restart after a crash can
# leave stale PIDs that confuse supervisor. Clear them so the tier starts clean.
rm -rf "$BENCH_DIR/logs/*.pid" 2>/dev/null || true

log "starting supervisor (gunicorn + workers + scheduler + scaled-socketio + nginx)"
exec "$@"
