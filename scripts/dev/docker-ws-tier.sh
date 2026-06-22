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
	local n="$1" lb_port="${WS_LB_HOST_PORT:-9000}" web_port="${WEBSERVER_PORT:-8000}" site="${SITE_NAME:-flock_os.localhost}"
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

	# Append an HTTP server block that proxies the web (gunicorn) service, so a k6
	# load generator running INSIDE the docker network can use ONE hostname
	# (ws-lb) for both the WS endpoint (:9000) and the realtime auth callback
	# (:8100 -> web). This clears the §8 ceiling #1 on the CLIENT side too: k6 in
	# a container reaches ws-lb over a real docker network transit, never the
	# host loopback (so no host ephemeral-port pressure, no `sudo sysctl`). The
	# realtime auth middleware's Host==Origin hostname check passes because both
	# sides resolve to `ws-lb`. (Host-based k6 on the Mac still works via the
	# published :9000 + :8100 ports; this block is additive.)
	#
	# The block must land INSIDE the `http {}` stanza — a bare `cat >>` would
	# place it after the closing brace (nginx: emerg "server directive is not
	# allowed here"). Reopen the rendered conf, drop the last top-level `}`
	# (the http close — events {} close sits above it and is not column-0 in the
	# template's structure, so the LAST `^}` is unambiguously http's), splice in
	# the new server block, then re-add the http close.
	local http_close
	http_close="$(awk '/^}$/ { n = NR } END { print n }' "$NGINX_CONF")"
	if [[ -z "$http_close" ]]; then
		err "rendered $NGINX_CONF has no top-level http close brace — refusing to splice HTTP proxy block"
		exit 1
	fi
	head -n $((http_close - 1)) "$NGINX_CONF" > "$NGINX_CONF.tmp"
	cat >> "$NGINX_CONF.tmp" <<NGINX

	# HTTP proxy to web (FLO-347 in-container k6 path) — added by docker-ws-tier.sh.
	# Listens on 8100 so the in-container k6 client (and the socketio worker's
	# realtime auth get_user_info callback) can use ONE hostname (ws-lb) for BOTH
	# the WS endpoint (:9000) and the HTTP auth/callback (:8100). The callback
	# target is web on its INTERNAL listen port (WEBSERVER_PORT, default 8000),
	# NOT 8100 — 8100 is nginx's own listen port, web doesn't bind it.
	#
	# proxy_set_header Host is set to the Frappe SITE name (not \$host = ws-lb)
	# because Frappe is a multi-site WSGI: it dispatches by Host header to pick
	# the site_config. From inside the docker network the inbound Host is
	# ws-lb:8100, which matches no site -> 404. Forcing Host to flock_os.localhost
	# (the provisioned site, SITE_NAME) makes web serve the right site for both
	# the k6 /api/method/login and the socketio worker get_user_info callback.
	server {
		listen 8100;
		server_name flock_os.localhost;
		location / {
			proxy_pass http://web:${web_port};
			proxy_set_header Host ${site};
			proxy_set_header X-Real-IP \$remote_addr;
			proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
		}
	}
}
NGINX
	mv "$NGINX_CONF.tmp" "$NGINX_CONF"
	log "rendered nginx LB conf -> $NGINX_CONF ($n upstream worker(s), LB host port $lb_port, +HTTP :8100 -> web:${web_port} [Host: ${site}])"
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
#
# By default the gate runs k6 INSIDE a container on the tier's backend network
# (FLOCK_GATE_MODE=incontainer). This is the only path that clears the full 15k
# bar without host-side ceilings: the k6 client reaches ws-lb over a real docker
# network transit, so neither the client->LB hop nor the LB->backend hop consumes
# host ephemeral ports or goes through Colima's docker-proxy (which saturates
# around ~3-4k concurrent published-port connections and takes the whole tier
# down with it). Set FLOCK_GATE_MODE=host for tiny smokes (< ~2k VUs) where the
# host path is fine and a local k6 install is handier.
cmd_gate() {
	require_docker
	ensure_env
	local vus="${1:-${FLOCK_GATE_VUS:-15000}}"
	local dur="${FLOCK_GATE_DURATION_SEC:-120}"
	local ramp="${FLOCK_GATE_RAMP_UP_SEC:-60}"
	local lb_port="${WS_LB_HOST_PORT:-9000}"
	local web_port="${WEBSERVER_HOST_PORT:-8000}"
	local site="${SITE_NAME:-flock_os.localhost}"
	local mode="${FLOCK_GATE_MODE:-incontainer}"
	local ts outdir
	ts="$(date -u +%Y%m%dT%H%M%SZ)"
	outdir="$REPO_ROOT/load/telemetry/${ts}-docker"
	mkdir -p "$outdir"

	# Resolve the tier's compose project network name. The base compose `name:` is
	# flock-ws and the network is `backend`, so the default is flock-ws_backend.
	local network="${FLOCK_NETWORK:-flock-ws_backend}"

	local base_url ws_origin ws_url k6_status=0
	# Smoke leader creds: passed through to k6 (loginSid). The docker tier
	# enforces Frappe's password policy, so FLOCK_PASSWORD in .env.docker is a
	# STRONGER value than the host bench default "flock" — both must be set.
	local flock_user="${FLOCK_USER:-leader@flock.os}"
	local flock_pw="${FLOCK_PASSWORD:-flock}"
	if [[ "$mode" == "incontainer" ]]; then
		# In-container k6: ONE hostname (ws-lb) for both WS (:9000) and the HTTP
		# auth/callback (:8100 -> web, via the nginx block render_nginx splices).
		# The realtime auth middleware's get_hostname(Host)==get_hostname(Origin)
		# check passes because both resolve to `ws-lb`. The get_user_info callback
		# (Origin + path) hits ws-lb:8100 -> nginx -> web:WEBSERVER_PORT.
		base_url="http://ws-lb:8100"
		ws_origin="$base_url"
		ws_url="ws://ws-lb:9000"
		log "running §8 gate (in-container k6): $vus VUs x ${dur}s -> $ws_url (auth $base_url) on network $network"
		# Pull the k6 image once (quiet), then run. The load/ dir is bind-mounted
		# so k6 sees config.js + lib/. --network attaches the container to the
		# tier's bridge so ws-lb/web resolve via docker DNS.
		docker pull -q "${FLOCK_K6_IMAGE:-grafana/k6:latest}" >/dev/null 2>&1 || true
		# Bind-mount load/ WRITABLE (not :ro) so k6 can drop the summary JSON
		# alongside the host-side k6-run.log. The outdir already exists on the
		# host (mkdir -p above) so it appears in-container at the matching path.
		docker run --rm --name flock-ws-gate \
			--network "$network" \
			-v "$REPO_ROOT/load:/scripts" \
			"${FLOCK_K6_IMAGE:-grafana/k6:latest}" run \
			-e WS_VUS="$vus" -e WS_DURATION_SEC="$dur" -e WS_RAMP_UP_SEC="$ramp" \
			-e WS_BASE_URL="$ws_url" -e BASE_URL="$base_url" -e WS_ORIGIN="$ws_origin" \
			-e FLOCK_USER="$flock_user" -e FLOCK_PASSWORD="$flock_pw" \
			--out "json=/scripts/telemetry/$(basename "$outdir")/k6-summary.json" \
			/scripts/ws_event_room.js 2>&1 | tee "$outdir/k6-run.log" || k6_status=$?
		# grafana/k6 writes the JSON output relative to its CWD; the bind mount
		# puts the file on the host at $outdir/k6-summary.json.
	else
		command -v k6 >/dev/null 2>&1 || { err "k6 not found (brew install k6)."; exit 1; }
		if ! nc -z 127.0.0.1 "$lb_port" 2>/dev/null; then
			err "ws-lb not reachable on :$lb_port — start the tier first: $PROG up"
			exit 1
		fi
		# Host-based k6 (smoke path only): point at the published ports. Origin
		# port MUST match the web container's internal listen port so the worker's
		# get_user_info callback resolves back to web via flock_os.localhost.
		base_url="http://$site:$web_port"
		ws_origin="$base_url"
		ws_url="ws://$site:$lb_port"
		log "running §8 gate (host k6): $vus VUs x ${dur}s -> $ws_url (web auth on :$web_port)"
		k6 run \
			-e WS_VUS="$vus" -e WS_DURATION_SEC="$dur" -e WS_RAMP_UP_SEC="$ramp" \
			-e WS_BASE_URL="$ws_url" -e BASE_URL="$base_url" -e WS_ORIGIN="$ws_origin" \
			-e FLOCK_USER="$flock_user" -e FLOCK_PASSWORD="$flock_pw" \
			--out "json=$outdir/k6-summary.json" \
			"$REPO_ROOT/load/ws_event_room.js" 2>&1 | tee "$outdir/k6-run.log" || k6_status=$?
	fi

	# Persist the exit status for the runbook (0 = all thresholds passed).
	echo "k6 exit status: $k6_status" > "$outdir/gate-status.txt"

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
