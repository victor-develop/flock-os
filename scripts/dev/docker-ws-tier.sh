#!/usr/bin/env bash
#
# Orchestrate the prod-equivalent docker WS tier for the clean §8 15k gate
# (FLO-347 / FLO-10 §8).
#
# This is the docker counterpart of scripts/dev/scale-socketio.sh: it stands up
# the SAME prod topology (N node socketio workers behind a nginx WS-upgrade LB,
# a DEDICATED adapter Redis via FLOCK_SIO_ADAPTER_REDIS) but as containers on a
# real docker network — which natively clears both dev-Mac ceilings documented
# in ws-broadcast-delivery.md -> Local testbed limits:
#   #1 loopback EADDRNOTAVAIL  (each container has its own ephemeral-port space;
#                               the LB<->backend hop is a real transit, not 2N
#                               ports from one loopback kernel)
#   #2 shared-Redis ETIMEDOUT  (the adapter gets its own redis-adapter container,
#                               set on every worker via FLOCK_SIO_ADAPTER_REDIS)
#
# The nginx LB conf is rendered from scripts/dev/nginx-socketio.conf.template
# (the SAME template scale-socketio.sh --lb nginx uses) with one
# `server socketio-N:9000;` per worker at the BACKENDS_INJECTED_HERE marker.
# The N socketio services are generated into docker/.runtime/ws-workers.yml and
# merged with docker/docker-compose.yml on `up`, so scaling is just
# `FLOCK_SIO_WORKERS=8 docker-ws-tier.sh up`.
#
# Usage:
#   scripts/dev/docker-ws-tier.sh up           # build (once) + start the tier
#   scripts/dev/docker-ws-tier.sh gate [vus]   # k6 §8 gate -> load/telemetry/<ts>/
#   scripts/dev/docker-ws-tier.sh status       # containers + ws-lb reachability
#   scripts/dev/docker-ws-tier.sh logs [svc]   # tail (default: ws-lb + a worker)
#   scripts/dev/docker-ws-tier.sh down         # stop + remove containers/network
#   scripts/dev/docker-ws-tier.sh down -v      # also drop the MariaDB volume
#
# Env (docker/.env.docker — see docker/.env.docker.example):
#   FLOCK_SIO_WORKERS    socketio worker count (default 4)
#   FLOCK_GATE_VUS       gate VUs (default 15000 — the full §8 bar)
#   FLOCK_GATE_DURATION_SEC  gate hold seconds (default 120)
#   WS_LB_HOST_PORT      host port for the nginx WS LB (default 9000)
#   FLOCK_IMAGE          image tag (default flock-os-frappe:ws-tier)
#
# Requires: docker + docker compose (Colima / Docker Desktop / OrbStack).
#
# Runbook: docs/development/ws-broadcast-delivery.md -> Prod-equivalent docker tier.
set -euo pipefail

PROG="$(basename "${BASH_SOURCE[0]}")"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DOCKER_DIR="$REPO_ROOT/docker"
COMPOSE_FILE="$DOCKER_DIR/docker-compose.yml"
WORKERS_FILE="$DOCKER_DIR/.runtime/ws-workers.yml"
NGINX_CONF="$DOCKER_DIR/.runtime/nginx.conf"
ENV_FILE="$DOCKER_DIR/.env.docker"
NGINX_TEMPLATE="$REPO_ROOT/scripts/dev/nginx-socketio.conf.template"

log() { echo "$PROG: $*"; }
err() { echo "$PROG: $*" >&2; }

require_docker() {
	command -v docker >/dev/null 2>&1 || {
		err "docker not found on PATH — install Colima (brew install colima docker docker-compose && colima start)"
		err "or Docker Desktop / OrbStack, then re-run."
		exit 1
	}
	docker compose version >/dev/null 2>&1 || {
		err "'docker compose' plugin unavailable — install it (brew install docker-compose) and retry."
		exit 1
	}
}

ensure_env() {
	if [[ ! -f "$ENV_FILE" ]]; then
		err "missing $ENV_FILE — create it from the example:"
		err "  cp docker/.env.docker.example docker/.env.docker  (and edit passwords)"
		exit 1
	fi
	set -a; # shellcheck disable=SC1090
	. "$ENV_FILE"; set +a
	: "${MARIADB_ROOT_PASSWORD:?MARIADB_ROOT_PASSWORD must be set in $ENV_FILE}"
}

# Render the nginx WS LB conf from the shared template. Reuses the SAME awk
# rendering as scale-socketio.sh start_nginx_lb, but with a container-writable
# state dir (/var/cache/nginx in nginx:alpine) and docker DNS backend names.
# `__STATE_DIR__` / `__LB_PORT__` are single-line substitutions; backends are
# injected at the standalone `# BACKENDS_INJECTED_HERE` marker via getline (a
# newline-laden awk -v value is rejected by BSD awk on macOS, so the backends
# list is written to a temp file and read with getline — exactly like the host
# script, for portability).
render_nginx() {
	local n="$1" lb_port="${WS_LB_HOST_PORT:-9000}"
	[[ -f "$NGINX_TEMPLATE" ]] || { err "nginx template missing: $NGINX_TEMPLATE"; exit 1; }
	mkdir -p "$DOCKER_DIR/.runtime"

	# One `server socketio-i:9000;` per worker. Every worker listens on internal
	# 9000 (each is in its own container network namespace), so the only thing
	# that differs is the docker DNS name. Plain round-robin is correct for the
	# ws-only smoke (same reasoning as the node LB — see the template comments).
	local bk="$DOCKER_DIR/.runtime/backends"
	: > "$bk"
	local i=1
	while (( i <= n )); do printf 'socketio-%s:9000\n' "$i" >>"$bk"; i=$((i + 1)); done

	# 1. placeholder substitution.
	awk -v state="/var/cache/nginx" -v lbport="$lb_port" '
		{ gsub(/__STATE_DIR__/, state); gsub(/__LB_PORT__/, lbport); print }
	' "$NGINX_TEMPLATE" > "$NGINX_CONF"
	# 2. inject the backend server lines at the marker line.
	awk -v backends="$bk" '
		/^[ \t]*# BACKENDS_INJECTED_HERE[ \t]*$/ {
			while ((getline line < backends) > 0) printf "\t\tserver %s;\n", line
			close(backends)
			next
		}
		{ print }
	' "$NGINX_CONF" > "$NGINX_CONF.tmp" && mv "$NGINX_CONF.tmp" "$NGINX_CONF"
	log "rendered nginx LB conf -> $NGINX_CONF ($n upstream worker(s), LB host port $lb_port)"
}

# Generate docker/.runtime/ws-workers.yml: one `socketio-i` service per worker,
# each pointed at the DEDICATED adapter Redis. Merged with the base compose on
# `up` via `-f`. All identical except the service name (each binds internal 9000
# in its own namespace). Keeping this generated (not hand-edited) means scaling
# is `FLOCK_SIO_WORKERS=8 docker-ws-tier.sh up`.
render_workers() {
	local n="$1"
	mkdir -p "$DOCKER_DIR/.runtime"
	{
		echo "# Generated by scripts/dev/docker-ws-tier.sh — do not edit (FLO-347)."
		echo "# $n socketio worker(s), each on internal :9000 behind ws-lb (nginx)."
		echo "services:"
		local i=1
		while (( i <= n )); do
			cat <<YAML
  socketio-$i:
    image: \${FLOCK_IMAGE:-flock-os-frappe:ws-tier}
    env_file: [.env.docker]
    environment:
      DB_HOST: mariadb
      DB_PORT: "3306"
      REDIS_CACHE: redis://redis-cache:6379
      REDIS_QUEUE: redis://redis-queue:6379
      REDIS_SOCKETIO: redis://redis-cache:6379
      # DEDICATED adapter Redis — clears testbed ceiling #2. The realtime adapter
      # wiring (resolveAdapterClients) honors this env over the shared redis_socketio.
      FLOCK_SIO_ADAPTER_REDIS: redis://redis-adapter:6379
      FRAPPE_SOCKETIO_PORT: "9000"
    command: ["bash", "-lc", "node apps/frappe/socketio.js"]
    # Each worker holds ~VUs/N concurrent sockets; raise nofile so the 15k gate
    # never hits a per-process fd ceiling (the macOS loopback ceiling this tier
    # clears is about ephemeral ports, but fd limits are a separate cap).
    ulimits:
      nofile: {soft: 1048576, hard: 1048576}
    volumes:
      - sites:/home/frappe/frappe-bench/sites
    depends_on:
      init: {condition: service_completed_successfully}
    networks: [backend]
YAML
			i=$((i + 1))
		done
	} > "$WORKERS_FILE"
	log "rendered $n socketio worker service(s) -> $WORKERS_FILE"
}

# docker compose invocation with both files + the env file. Direct -f flags
# (not xargs/NUL: bash strips NULs in command substitution, which silently
# concatenates the paths into one bogus -f argument). The worker overlay is
# always present after render_workers(); --env-file supplies the interpolation
# vars (${MARIADB_ROOT_PASSWORD:?} etc.) that compose can't read from the
# services' own `env_file:` (that is container-runtime env, not interpolation).
dc() {
	local -a f=("-f" "$COMPOSE_FILE")
	[[ -f "$WORKERS_FILE" ]] && f+=("-f" "$WORKERS_FILE")
	docker compose "${f[@]}" --env-file "$ENV_FILE" "$@"
}

cmd_up() {
	require_docker
	ensure_env
	local n="${FLOCK_SIO_WORKERS:-4}"
	if ! [[ "$n" =~ ^[0-9]+$ ]] || (( n < 1 )); then err "FLOCK_SIO_WORKERS='$n' invalid"; exit 2; fi
	render_nginx "$n"
	render_workers "$n"
	log "bringing the tier up ($n socketio worker(s)) — first run builds the image ..."
	dc up -d --build
	echo
	log "tier starting. WS endpoint: ws://flock_os.localhost:${WS_LB_HOST_PORT:-9000}"
	log "check readiness: $PROG status    | run the gate: $PROG gate"
}

cmd_down() {
	require_docker
	local remove_volumes=0
	[[ "${1:-}" == "-v" ]] && remove_volumes=1
	if (( remove_volumes )); then dc down -v; else dc down; fi
	log "tier down."
}

cmd_status() {
	require_docker
	dc ps
	echo
	local lb_port="${WS_LB_HOST_PORT:-9000}"
	if nc -z 127.0.0.1 "$lb_port" 2>/dev/null; then
		log "ws-lb reachable on 127.0.0.1:$lb_port"
	else
		log "ws-lb NOT reachable on 127.0.0.1:$lb_port (still starting? try '$PROG logs ws-lb')"
	fi
}

cmd_logs() {
	require_docker
	local svc="${1:-ws-lb}"
	shift || true
	dc logs -f "$svc" "$@"
}

# Run the k6 §8 gate against the live tier and capture the full evidence bundle
# (k6 summary JSON + the stdout run) under load/telemetry/<timestamp>-docker/,
# the same place the host runs land, so a clean run is a drop-in work product.
cmd_gate() {
	require_docker
	ensure_env
	command -v k6 >/dev/null 2>&1 || { err "k6 not found (brew install k6)."; exit 1; }
	local vus="${1:-${FLOCK_GATE_VUS:-15000}}"
	local dur="${FLOCK_GATE_DURATION_SEC:-120}"
	local lb_port="${WS_LB_HOST_PORT:-9000}"
	local web_port="${WEBSERVER_HOST_PORT:-8000}"
	local site="${SITE_NAME:-flock_os.localhost}"
	local ts outdir
	ts="$(date -u +%Y%m%dT%H%M%SZ)"
	outdir="$REPO_ROOT/load/telemetry/${ts}-docker"
	mkdir -p "$outdir"

	log "running §8 gate: $vus VUs x ${dur}s -> ws://$site:$lb_port (web auth on :$web_port)"
	if ! nc -z 127.0.0.1 "$lb_port" 2>/dev/null; then
		err "ws-lb not reachable on :$lb_port — start the tier first: $PROG up"
		exit 1
	fi

	# Point k6 at this tier's published ports: WS -> ws-lb, HTTP login + the
	# realtime auth Origin -> web. The Origin port MUST match the web container's
	# internal listen port (WEBSERVER_PORT == WEBSERVER_HOST_PORT in the default
	# env) so the socketio worker's get_user_info callback resolves back to the
	# web container via the flock_os.localhost network alias.
	local base_url="http://$site:$web_port"
	local ws_origin="$base_url"
	local ws_url="ws://$site:$lb_port"

	# The k6 smoke defaults (load/config.js) already target ws://<site>:9000;
	# override the ports for a non-default web/LB mapping. The summary JSON +
	# a full stdout capture are the gate evidence.
	k6 run \
		-e WS_VUS="$vus" -e WS_DURATION_SEC="$dur" \
		-e WS_BASE_URL="$ws_url" -e BASE_URL="$base_url" -e WS_ORIGIN="$ws_origin" \
		"$REPO_ROOT/load/ws_event_room.js" 2>&1 | tee "$outdir/k6-run.log" || true

	k6 run \
		-e WS_VUS="$vus" -e WS_DURATION_SEC="$dur" \
		-e WS_BASE_URL="$ws_url" -e BASE_URL="$base_url" -e WS_ORIGIN="$ws_origin" \
		--out "json=$outdir/k6-summary.json" \
		"$REPO_ROOT/load/ws_event_room.js" >"$outdir/k6-summary-run.log" 2>&1 || true

	log "gate evidence captured under $outdir"
	log "key signals: flock_ws_connect_duration p(95), flock_ws_broadcast_latency p(95),"
	log "              flock_ws_receive_errors (target == 0), sessions established."
}

usage() { sed -n '2,/^$/p' "${BASH_SOURCE[0]}" >&2; }

main() {
	local cmd="${1:-}"
	[[ $# -gt 0 ]] && shift || true
	case "$cmd" in
		up) cmd_up "$@" ;;
		down) cmd_down "$@" ;;
		status) cmd_status "$@" ;;
		logs) cmd_logs "$@" ;;
		gate) cmd_gate "$@" ;;
		render) # internal: render nginx + workers without starting (for CI/syntax checks)
			ensure_env; render_nginx "${FLOCK_SIO_WORKERS:-4}"; render_workers "${FLOCK_SIO_WORKERS:-4}" ;;
		""|-h|--help|help) usage ;;
		*) err "unknown command '$cmd'"; exit 2 ;;
	esac
}

main "$@"
