#!/usr/bin/env bash
#
# Container entrypoint for the flock_os Frappe bench image (FLO-347).
#
# Renders sites/common_site_config.json from compose env (DB host, Redis service
# names, ports) so the SAME image runs on any docker network without a rebuild,
# then execs the compose `command:` (gunicorn / node socketio / scheduler). The
# dedicated adapter Redis is NOT a Frappe conf key — it is read directly by the
# realtime adapter wiring via FLOCK_SIO_ADAPTER_REDIS (set per socketio worker).
#
# The shape mirrors the host bench's common_site_config.json (redis_cache /
# redis_queue / redis_socketio + socketio_port + webserver_port), with the host
# 127.0.0.1 IPs replaced by the compose service names so traffic stays on the
# docker network.
set -euo pipefail

BENCH_DIR="${BENCH_DIR:-/home/frappe/frappe-bench}"
SITES_DIR="$BENCH_DIR/sites"
mkdir -p "$SITES_DIR"

SITE_NAME="${SITE_NAME:-flock_os.localhost}"

# The sites/ dir is a shared named volume across init/web/workers, so it starts
# EMPTY (hiding the image-baked apps.txt). Frappe's get_all_apps() reads
# sites/apps.txt (one app per line) to build its module map; BOTH frappe and
# flock_os must be listed or gunicorn's site resolution breaks ("site does not
# exist"). Deterministic two-line file (bench init writes apps.txt without a
# trailing newline, which would otherwise join `frappe`+`flock_os` into the
# bogus module `frappeflock_os`).
printf 'frappe\nflock_os\n' > "$SITES_DIR/apps.txt"

# Compose service hostnames (overridable for a non-default compose project name).
DB_HOST="${DB_HOST:-mariadb}"
DB_PORT="${DB_PORT:-3306}"
REDIS_CACHE="${REDIS_CACHE:-redis://redis-cache:6379}"
REDIS_QUEUE="${REDIS_QUEUE:-redis://redis-queue:6379}"
# Frappe's own socketio pub/sub ("events" channel) reuses the cache instance on
# the dev bench; in the prod-equivalent tier it gets its own to avoid contending
# with the adapter Redis. Defaults to the cache service.
REDIS_SOCKETIO="${REDIS_SOCKETIO:-$REDIS_CACHE}"

cat > "$SITES_DIR/common_site_config.json" <<JSON
{
 "db_host": "$DB_HOST",
 "db_port": $DB_PORT,
 "redis_cache": "$REDIS_CACHE",
 "redis_queue": "$REDIS_QUEUE",
 "redis_socketio": "$REDIS_SOCKETIO",
 "socketio_port": ${FRAPPE_SOCKETIO_PORT:-9000},
 "webserver_port": ${WEBSERVER_PORT:-8000},
 "default_site": "$SITE_NAME",
 "frappe_user": "frappe",
 "serve_default_site": true,
 "use_redis_auth": false,
 "restart_supervisor_on_update": false,
 "restart_systemd_on_update": false,
 "shallow_clone": true,
 "rebase_on_pull": false,
 "live_reload": false,
 "gunicorn_workers": ${GUNICORN_WORKERS:-4}
}
JSON

# The first socketio worker (or the `init` one-shot) creates the site once the
# DB is up. The compose `web` service runs `init-site` before gunicorn (see
# docker-compose.yml command), so a plain role container can assume the site
# exists by the time it serves traffic.

cd "$BENCH_DIR"
exec "$@"
