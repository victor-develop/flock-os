#!/usr/bin/env bash
#
# scripts/deploy/tunnel-staging.sh — $0 Cloudflare Tunnel staging URL (FLO-889).
#
# Why this exists: the board has been silent on FLO-872 (paid Frappe Cloud), so
# a `cloudflared` quick tunnel fronts the already-proven 15k docker WS tier
# (FLO-775) to produce a reachable, auto-TLS staging URL with NO account, NO
# domain, NO credit card, NO human action. This satisfies the FLO-249 staging
# acceptance gate (scripts/deploy/smoke-staging.sh PASS) and unblocks the
# downstream chain (FLO-250 promotion gate) without waiting on the board.
#
# Architecture: a quick tunnel exposes ONE local port, and smoke-staging.sh
# expects socketio at the SAME origin (/socket.io/). So this script runs a tiny
# "edge" nginx (container) that merges both surfaces onto one origin, then
# points ONE cloudflared quick tunnel at it. cloudflared itself ALSO runs in a
# container on the same docker network, talking to the edge over docker DNS
# (http://flock-tunnel-edge:80) — no published host port needed for the tunnel:
#
#   browser --https/wss--> Cloudflare edge --(quic)--> cloudflared container
#     -> edge nginx (:80)
#          /socket.io/  -> ws-lb:9000   (FLO-121 N-worker sticky-L7 WS tier)
#          /assets/     -> sites/assets (disk)
#          /            -> web:8000     (gunicorn / Frappe web)
#
# Why cloudflared runs in a container (not as a host process):
#   1. The Mac's launchd `com.cloudflare.cloudflared` service fronts the
#      Paperclip control plane via ~/.cloudflared/config.yml (named tunnel
#      84ce11f0, catch-all http_status:404). A host `cloudflared tunnel --url`
#      AUTO-LOADS that config, reuses the named tunnel, and its catch-all 404
#      SHADOWS --url — every request 404s and never reaches the edge. The
#      container has no such config, so --url owns the quick tunnel cleanly.
#      NEVER kill the launchd service or delete ~/.cloudflared/config.yml — it
#      serves Paperclip itself.
#   2. A detached container survives agent-harness shell reaping that kills
#      backgrounded host processes (host `nohup cloudflared &` gets SIGTERM'd
#      when the spawning shell exits).
#
# This is an INTERIM staging validation path, not production. The quick-tunnel
# URL is ephemeral (changes every `up`; the Mac must stay running). It is good
# for staging smoke + rollback drills; always-on production still needs the real
# Frappe Cloud VM (FLO-872).
#
# Usage:
#   scripts/deploy/tunnel-staging.sh up            # edge + cloudflared -> prints URL
#   scripts/deploy/tunnel-staging.sh status        # containers + URL + reachability
#   scripts/deploy/tunnel-staging.sh smoke [url]   # smoke-staging.sh vs the URL
#   scripts/deploy/tunnel-staging.sh logs          # tail cloudflared logs
#   scripts/deploy/tunnel-staging.sh down          # stop cloudflared + edge containers
#
# Env:
#   EDGE_HOST_PORT        host port published for the edge (debug only; default 8090)
#   SITE_NAME             Frappe site name / docker-local Host (default flock_os.localhost)
#   FLOCK_NETWORK         docker network the WS tier runs on (default flock-ws_backend)
#   FLOCK_SITES_VOLUME    shared sites volume name (default flock-ws_sites)
#   FLOCK_CLOUDFLARED_IMAGE  cloudflared image (default cloudflare/cloudflared:latest)
#   WEB_PORT              internal gunicorn port (default 8000)
#
# Prereqs: Docker + the prod-equivalent docker WS tier up
#   (scripts/dev/docker-ws-tier.sh up).
#
# Runbook: docs/development/tunnel-staging.md
set -euo pipefail

PROG="$(basename "${BASH_SOURCE[0]}")"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RUNTIME_DIR="$REPO_ROOT/docker/.runtime"
EDGE_CONF="$RUNTIME_DIR/tunnel-edge.conf"
EDGE_CTR="flock-tunnel-edge"
CF_CTR="flock-tunnel-cloudflared"
CF_LOG_HOST="$RUNTIME_DIR/tunnel-cloudflared.log"
URL_FILE="$RUNTIME_DIR/tunnel-url.txt"

EDGE_HOST_PORT="${EDGE_HOST_PORT:-8090}"
SITE_NAME="${SITE_NAME:-flock_os.localhost}"
FLOCK_NETWORK="${FLOCK_NETWORK:-flock-ws_backend}"
FLOCK_SITES_VOLUME="${FLOCK_SITES_VOLUME:-flock-ws_sites}"
FLOCK_CLOUDFLARED_IMAGE="${FLOCK_CLOUDFLARED_IMAGE:-cloudflare/cloudflared:latest}"
WEB_PORT="${WEBSERVER_PORT:-8000}"

log() { echo "$PROG: $*"; }
err() { echo "$PROG: $*" >&2; }

require_docker() {
	command -v docker >/dev/null 2>&1 || { err "docker not found on PATH."; exit 1; }
}

# Ensure the prod-equivalent docker WS tier (web + ws-lb) is up — the edge
# proxies to those services by docker-DNS name, so they MUST be running on the
# same network. Surface a clear instruction if not.
require_ws_tier() {
	if ! docker ps --format '{{.Names}}' | grep -qx 'flock-ws-web-1' \
		|| ! docker ps --format '{{.Names}}' | grep -qE 'flock-ws-ws-lb-1'; then
		err "the prod-equivalent docker WS tier is not running."
		err "start it first:  scripts/dev/docker-ws-tier.sh up"
		exit 1
	fi
}

# Render the edge nginx conf. Merges Frappe web (gunicorn) + the scaled-socketio
# tier onto a single :80 origin so one quick-tunnel URL serves both the app and
# the WS upgrade. The /socket.io/ block rewrites Host+Origin to the docker-local
# site so (a) Frappe realtime auth get_hostname(Host)==get_hostname(Origin) and
# namespace checks pass, and (b) get_url() builds the get_user_info callback from
# Origin -> http://<SITE_NAME>:<WEB_PORT>, which resolves to `web` via its
# network alias (callback stays on the docker network, no public-edge hairpin).
render_edge_conf() {
	mkdir -p "$RUNTIME_DIR"
	cat > "$EDGE_CONF" <<NGINX
# Generated by $PROG (FLO-889) — single-origin edge for the cloudflared quick
# tunnel. Serves Frappe web + scaled-socketio on one :80 listener.
worker_processes auto;
pid /tmp/nginx.pid;
error_log /dev/stderr warn;

events { worker_connections 8192; }

http {
	include /etc/nginx/mime.types;
	default_type application/octet-stream;
	sendfile on; tcp_nopush on; tcp_nodelay on;
	keepalive_timeout 65; server_tokens off;
	client_max_body_size 64m;

	upstream flock_web { server web:${WEB_PORT} max_fails=3 fail_timeout=10s; keepalive 16; }
	upstream flock_ws_lb { server ws-lb:9000; keepalive 32; }

	server {
		listen 80 default_server;
		server_name _;

		location /socket.io/ {
			proxy_pass http://flock_ws_lb;
			proxy_http_version 1.1;
			proxy_set_header Upgrade \$http_upgrade;
			proxy_set_header Connection "upgrade";
			proxy_set_header Host ${SITE_NAME};
			proxy_set_header Origin http://${SITE_NAME}:${WEB_PORT};
			proxy_set_header X-Real-IP \$remote_addr;
			proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
			proxy_read_timeout 3600s;
			proxy_send_timeout 3600s;
			proxy_buffering off;
		}

		location /assets/ {
			alias /home/frappe/frappe-bench/sites/assets/;
			expires 1h; access_log off;
		}
		location /files/ {
			root /home/frappe/frappe-bench/sites;
			expires 1d; access_log off;
		}

		location / {
			proxy_pass http://flock_web;
			proxy_http_version 1.1;
			proxy_set_header Host ${SITE_NAME};
			proxy_set_header X-Real-IP \$remote_addr;
			proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
			proxy_set_header X-Forwarded-Proto \$scheme;
			proxy_read_timeout 120s; proxy_send_timeout 120s;
			proxy_buffering off;
		}
	}
}
NGINX
	log "rendered edge nginx conf -> $EDGE_CONF (site=${SITE_NAME}, web=:${WEB_PORT})"
}

start_edge() {
	render_edge_conf
	docker rm -f "$EDGE_CTR" >/dev/null 2>&1 || true
	# Attach to the WS tier's network so `web` / `ws-lb` resolve by docker DNS,
	# and mount the shared sites volume read-only so /assets/ is served from disk
	# (the prod nginx-in-front-of-gunicorn pattern; gunicorn does not add
	# SharedDataMiddleware, so /assets/ would 404 without this — FLO-882 gate 4).
	# Publish :80 on 127.0.0.1:${EDGE_HOST_PORT} for local debugging only — the
	# cloudflared container reaches the edge over docker DNS, not this port.
	docker run -d --name "$EDGE_CTR" \
		--network "$FLOCK_NETWORK" \
		-v "${FLOCK_SITES_VOLUME}:/home/frappe/frappe-bench/sites:ro" \
		-p "127.0.0.1:${EDGE_HOST_PORT}:80" \
		-v "$EDGE_CONF:/etc/nginx/nginx.conf:ro" \
		nginx:alpine >/dev/null
	sleep 2
	if ! docker ps --format '{{.Names}}' | grep -qx "$EDGE_CTR"; then
		err "edge container $EDGE_CTR failed to start — logs:"
		docker logs "$EDGE_CTR" >&2 || true
		exit 1
	fi
	log "edge nginx up on 127.0.0.1:${EDGE_HOST_PORT} (container $EDGE_CTR on $FLOCK_NETWORK)"
}

# Scrape the trycloudflare URL out of cloudflared's startup log. Tries for up to
# ~30s (a fresh quick tunnel can take a few seconds to register + advertise).
wait_for_url() {
	local log="$1" url=""
	for _ in $(seq 1 30); do
		url="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$log" 2>/dev/null | head -1 || true)"
		[[ -n "$url" ]] && break
		sleep 1
	done
	echo "$url"
}

start_cloudflared() {
	docker rm -f "$CF_CTR" >/dev/null 2>&1 || true
	# Run cloudflared on the WS tier's network so it can reach the edge at its
	# container DNS name (flock-tunnel-edge:80) over the docker network — no host
	# port publish needed for the tunnel path. The container has no
	# ~/.cloudflared/config.yml, so --url owns the quick tunnel + ingress with no
	# collision with the host's launchd Paperclip tunnel.
	docker run -d --name "$CF_CTR" --network "$FLOCK_NETWORK" \
		--restart no \
		"$FLOCK_CLOUDFLARED_IMAGE" \
		tunnel --no-autoupdate --url "http://${EDGE_CTR}:80" >/dev/null
}

cmd_up() {
	require_docker
	require_ws_tier
	start_edge
	log "starting cloudflared quick tunnel (container $CF_CTR) -> http://${EDGE_CTR}:80 ..."
	start_cloudflared

	# Mirror the container's log to the host file so status/smoke can scrape the
	# URL without re-reading docker logs each time.
	local url=""
	for _ in $(seq 1 30); do
		docker logs "$CF_CTR" > "$CF_LOG_HOST" 2>&1 || true
		url="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$CF_LOG_HOST" 2>/dev/null | head -1 || true)"
		[[ -n "$url" ]] && break
		sleep 1
	done
	if [[ -z "$url" ]]; then
		err "cloudflared did not advertise a trycloudflare URL within 30s — logs:"
		docker logs "$CF_CTR" >&2 || true
		exit 1
	fi
	echo "$url" > "$URL_FILE"

	# Wait for the quick-tunnel hostname's DNS to publish BEFORE probing with the
	# system resolver. Querying the system resolver (curl) while Cloudflare has
	# not yet published the A record seeds a NEGATIVE cache entry on macOS
	# (mDNSResponder), which then blocks the host for minutes even after the
	# record is live. Polling a public resolver (1.1.1.1) avoids that: once it
	# sees the record, the system resolver will too (no stale negative entry).
	local host="${url#https://}"
	log "waiting for DNS on ${host} to publish ..."
	local dns_ok=0
	for _ in $(seq 1 30); do
		if dig +short +time=2 +tries=1 "$host" @1.1.1.1 2>/dev/null | grep -qE '^[0-9]+\.'; then dns_ok=1; break; fi
		sleep 2
	done
	if (( ! dns_ok )); then
		err "DNS for ${host} did not publish within 60s (Cloudflare quick-tunnel throttle?)."
		err "The URL may still come good — retry:  $PROG status"
	fi

	# Now poll the Frappe ping endpoint (system resolver is safe once DNS is live).
	log "waiting for the tunnel to route ..."
	local ok=0
	for _ in $(seq 1 24); do
		if curl -sf -m 10 "${url}/api/method/ping" >/dev/null 2>&1; then ok=1; break; fi
		sleep 2
	done

	echo
	log "tunnel is UP. Staging URL:"
	echo "  $url"
	if (( ok )); then
		log "reachability check: OK (/api/method/ping -> pong)"
	else
		err "reachability check timed out — the URL may still be propagating;"
		err "retry in a few seconds:  $PROG status   (or curl -s ${url}/api/method/ping)"
	fi
	echo
	log "run the acceptance gate:  $PROG smoke"
	log "tail logs:                $PROG logs"
	log "tear down:                $PROG down"
}

cmd_down() {
	require_docker
	docker rm -f "$CF_CTR" "$EDGE_CTR" >/dev/null 2>&1 || true
	rm -f "$URL_FILE"
	log "tunnel staging torn down (cloudflared + edge containers removed)."
	log "the prod-equivalent docker WS tier is still running — take it down with:"
	log "  scripts/dev/docker-ws-tier.sh down"
}

current_url() {
	local url=""
	[[ -f "$URL_FILE" ]] && url="$(cat "$URL_FILE")"
	if [[ -z "$url" ]]; then
		docker logs "$CF_CTR" > "$CF_LOG_HOST" 2>&1 || true
		url="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$CF_LOG_HOST" 2>/dev/null | head -1 || true)"
	fi
	echo "$url"
}

cmd_status() {
	require_docker
	echo "== edge container =="
	if docker ps --format '{{.Names}}\t{{.Status}}\t{{.Ports}}' | grep -q "$EDGE_CTR"; then
		docker ps --format '{{.Names}}\t{{.Status}}\t{{.Ports}}' | grep "$EDGE_CTR"
	else
		echo "$EDGE_CTR not running"
	fi
	echo "== cloudflared container =="
	if docker ps --format '{{.Names}}\t{{.Status}}' | grep -q "$CF_CTR"; then
		docker ps --format '{{.Names}}\t{{.Status}}' | grep "$CF_CTR"
	else
		echo "$CF_CTR not running"
	fi
	echo "== URL =="
	local url; url="$(current_url)"
	if [[ -n "$url" ]]; then
		echo "$url"
		local code
		code="$(curl -s -o /dev/null -w '%{http_code}' -m 10 "${url}/api/method/ping" 2>/dev/null || echo 000)"
		echo "ping: HTTP $code (200 = healthy)"
	else
		echo "no tunnel URL found — run: $PROG up"
	fi
}

cmd_smoke() {
	local url="${1:-}"
	[[ -z "$url" ]] && url="$(current_url)"
	if [[ -z "$url" ]]; then err "no tunnel URL — run: $PROG up"; exit 1; fi
	log "running staging smoke against $url"
	exec env STAGING_URL="$url" "$REPO_ROOT/scripts/deploy/smoke-staging.sh" "${@:2}"
}

cmd_logs() {
	require_docker
	log "tailing cloudflared (Ctrl-C to detach; tunnel keeps running)."
	log "edge nginx: 'docker logs -f $EDGE_CTR'"
	docker logs -f "$CF_CTR"
}

usage() { sed -n '2,/^$/p' "${BASH_SOURCE[0]}" >&2; }

main() {
	local cmd="${1:-}"
	[[ $# -gt 0 ]] && shift || true
	case "$cmd" in
		up) cmd_up "$@" ;;
		down) cmd_down "$@" ;;
		status) cmd_status "$@" ;;
		smoke) cmd_smoke "$@" ;;
		logs) cmd_logs "$@" ;;
		""|-h|--help|help) usage ;;
		*) err "unknown command '$cmd'"; exit 2 ;;
	esac
}

main "$@"
