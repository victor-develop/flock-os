#!/usr/bin/env bash
#
# Scale the Frappe socketio tier horizontally for the §8 15k WS connection-setup
# gate (FLO-121 / FLO-10 §8).
#
# The §8 wall (FLO-121) is a single-process node socketio connection-setup wall:
# one event loop serializes ~15k concurrent handshakes (connect p95 ~27 s, <1 %
# established). The auth cache (FLO-116) cleared the auth-callback wall; this
# clears the connection-setup wall by running N node socketio processes behind a
# WS-aware load balancer (scripts/dev/socketio-lb.js) so the handshakes spread.
#
# What `start` does:
#   1. ensures `@socket.io/redis-adapter` is installed (npm install at repo root);
#   2. frees the LB port by stopping whatever holds it (the single socketio);
#   3. launches N socketio backends on 9001..900N (`node apps/frappe/socketio.js`
#      with FRAPPE_SOCKETIO_PORT set per process), each wired with the redis
#      adapter (wire-socketio-redis-adapter.sh must have run / migrate re-runs it);
#   4. launches the round-robin TCP LB on the LB port (default 9000, the smoke's
#      default WS_BASE_URL, so k6 needs no override).
#
# The smoke uses `transport=websocket` only → one independent TCP connection per
# client → plain round-robin distributes cleanly, no sticky sessions needed. Cross-
# backend fan-out is the redis adapter + the existing replicated "events" pub/sub.
#
# Usage:
#   scripts/dev/scale-socketio.sh start   [N]   # bring up the scaled tier (N defaults to FLOCK_SIO_PROCESSES / nproc)
#   scripts/dev/scale-socketio.sh stop          # tear down backends + LB
#   scripts/dev/scale-socketio.sh restart [N]   # stop + start
#   scripts/dev/scale-socketio.sh status        # pids + per-backend connection distribution
#
# Env:
#   FLOCK_SIO_PROCESSES   backend count (default: nproc, capped at 8)
#   FLOCK_SIO_BASE_PORT   first backend port (default 9001)
#   FLOCK_SIO_LB_PORT     LB listen port (default 9000 — the smoke default)
#   FLOCK_SIO_LB          LB kind: "node" (default — the dependency-free TCP
#                         round-robin proxy scripts/dev/socketio-lb.js) or "nginx"
#                         (the production WS-upstream shape; see
#                         scripts/dev/nginx-socketio.conf.template). `start --lb
#                         nginx` overrides. nginx must be on PATH when selected.
#   FLOCK_SIO_ADAPTER_REDIS  optional `redis://` URL for a DEDICATED adapter Redis.
#                         Backends inherit this process's env, so the redis-adapter
#                         wiring binds to it instead of the shared `redis_socketio`
#                         (FLO-127 §2 unblock). Bring one up with
#                         scripts/dev/start-adapter-redis.sh.
#   FLOCK_SIO_BENCH       bench root (default: same resolution as the wire scripts)
#   STATS_INTERVAL_MS     LB per-backend stats cadence (default 5000; 0 = off)
#
# Runbook: docs/development/ws-broadcast-delivery.md -> Scaling the socketio tier.
set -euo pipefail

PROG="$(basename "${BASH_SOURCE[0]}")"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

if [[ -n "${FLOCK_SIO_BENCH:-}" ]]; then
	BENCH="$FLOCK_SIO_BENCH"
else
	BENCH="${FRAPPE_BENCH_ROOT:-$REPO_ROOT/../flock-os-bench}"
fi
SOCKETIO_JS="$BENCH/apps/frappe/socketio.js"
if [[ ! -f "$SOCKETIO_JS" ]]; then
	echo "$PROG: bench socketio.js not found at: $SOCKETIO_JS" >&2
	echo "$PROG: set FLOCK_SIO_BENCH or FRAPPE_BENCH_ROOT" >&2
	exit 1
fi

BASE_PORT="${FLOCK_SIO_BASE_PORT:-9001}"
LB_PORT="${FLOCK_SIO_LB_PORT:-9000}"
NPROC="$(sysctl -n hw.logicalcpu 2>/dev/null || echo 4)"
DEFAULT_N="$NPROC"; (( DEFAULT_N > 8 )) && DEFAULT_N=8
# N is resolved INSIDE cmd_start (from its own positional arg, NOT the subcommand
# in $1 at this scope — `scale-socketio.sh start 4` puts "start" in $1 here).

STATE_DIR="$BENCH/logs/scaled-socketio"
BACKENDS_FILE="$STATE_DIR/backends"
LB_PID_FILE="$STATE_DIR/lb.pid"
LB_LOG="$STATE_DIR/lb.log"

log() { echo "$PROG: $*"; }
err() { echo "$PROG: $*" >&2; }

ensure_adapter_dep() {
	# `@socket.io/redis-adapter` resolves from the flock_os app root (the repo
	# root, symlinked into bench/apps/flock_os). Install it there if missing so the
	# wired backends can attach the adapter. Idempotent + offline when present.
	if node -e "require.resolve('@socket.io/redis-adapter',{paths:[process.argv[1]]})" "$REPO_ROOT" >/dev/null 2>&1; then
		return 0
	fi
	log "installing @socket.io/redis-adapter at $REPO_ROOT (one-time)"
	if ! (cd "$REPO_ROOT" && npm install --no-audit --no-fund); then
		err "npm install failed — the tier will run without the adapter (single-process fan-out only)."
		err "install manually: (cd $REPO_ROOT && npm install)"
		return 1
	fi
}

# Free a TCP port by killing whatever holds it (the single-process socketio, or a
# leftover backend/LB). Best-effort; warns if nothing held it.
free_port() {
	local port="$1" pids
	pids="$(lsof -ti tcp:"$port" 2>/dev/null || true)"
	if [[ -n "$pids" ]]; then
		log "freeing :$port (held by pid: ${pids//$'\n'/,})"
		kill $pids 2>/dev/null || true
		# Wait for the listener to actually go away so the next bind does not EADDRINUSE.
		for _ in 1 2 3 4 5 6 7 8 9 10; do
			{ lsof -ti tcp:"$port" >/dev/null 2>&1 || break; } && sleep 0.5
		done
	fi
}

wait_for_port() {
	# Wait until something is listening on :port (a backend boot gate).
	local port="$1" i
	for ((i = 0; i < 40; i++)); do
		if nc -z 127.0.0.1 "$port" 2>/dev/null; then return 0; fi
		if [[ ! -e /dev/null ]]; then :; fi
		sleep 0.5
	done
	return 1
}

# Launch the dependency-free node round-robin TCP proxy on the LB port. This is
# the default LB kind (FLOCK_SIO_LB=node) and the original FLO-121 local tier.
start_node_lb() {
	local backends
	backends="$(paste -sd, "$BACKENDS_FILE")"
	PORT="$LB_PORT" BACKENDS="$backends" STATS_INTERVAL_MS="${STATS_INTERVAL_MS:-5000}" \
		node "$REPO_ROOT/scripts/dev/socketio-lb.js" >"$LB_LOG" 2>&1 &
	local lbpid=$!
	echo "$lbpid" > "$LB_PID_FILE"
	echo "node" > "$STATE_DIR/lb-kind"
	log "LB (node) pid=$lbpid on :$LB_PORT (log: $LB_LOG)"
	if ! wait_for_port "$LB_PORT"; then
		err "LB did not bind :$LB_PORT — check $LB_LOG"
		cmd_stop >/dev/null 2>&1 || true
		exit 1
	fi
}

# Launch the production-shape nginx WS-upstream LB (FLOCK_SIO_LB=nginx). Renders
# scripts/dev/nginx-socketio.conf.template with the live backend list. nginx must
# be on PATH — this is the prod topology, not the dev default.
start_nginx_lb() {
	command -v nginx >/dev/null 2>&1 || {
		err "nginx not found on PATH — required for --lb nginx (the prod WS shape)."
		err "install it (brew install nginx) or omit --lb to use the node round-robin proxy."
		exit 1
	}
	local tmpl="$REPO_ROOT/scripts/dev/nginx-socketio.conf.template"
	[[ -f "$tmpl" ]] || { err "nginx template missing: $tmpl"; exit 1; }
	local conf="$STATE_DIR/nginx.conf"
	# 1. Render the __STATE_DIR__ / __LB_PORT__ placeholders (single-line -v
	#    values, safe under BSD awk / gawk alike).
	awk -v state="$STATE_DIR" -v lbport="$LB_PORT" '
		{ gsub(/__STATE_DIR__/, state); gsub(/__LB_PORT__/, lbport); print }
	' "$tmpl" > "$conf"
	# 2. Inject one `server host:port;` per backend at the standalone marker.
	#    Reads the backend list via getline (portable: a newline-laden value in
	#    `awk -v` is rejected by BSD awk on macOS, which silently dropped every
	#    server line) and anchors on the marker line ALONE — `^[ \t]*# ... $` — so
	#    the template's header prose that merely mentions the marker is untouched.
	awk -v backends="$BACKENDS_FILE" '
		/^[ \t]*# BACKENDS_INJECTED_HERE[ \t]*$/ {
			while ((getline line < backends) > 0) printf "\t\tserver %s;\n", line
			close(backends)
			next
		}
		{ print }
	' "$conf" > "$conf.tmp" && mv "$conf.tmp" "$conf"

	echo "nginx" > "$STATE_DIR/lb-kind"
	nginx -c "$conf" -p "$STATE_DIR/" 2>>"$STATE_DIR/nginx-start.log" || {
		err "nginx failed to start — check $STATE_DIR/nginx-error.log and $STATE_DIR/nginx-start.log"
		exit 1
	}
	log "LB (nginx) on :$LB_PORT (conf: $conf)"
	if ! wait_for_port "$LB_PORT"; then
		err "nginx did not bind :$LB_PORT — check $STATE_DIR/nginx-error.log"
		cmd_stop >/dev/null 2>&1 || true
		exit 1
	fi
}

cmd_start() {
	local LB_KIND="${FLOCK_SIO_LB:-node}"
	local N=""
	# Positional N + optional `--lb {node,nginx}` flag, in any order. `--lb`
	# overrides the FLOCK_SIO_LB env (the production-shape opt-in).
	while [[ $# -gt 0 ]]; do
		case "$1" in
			--lb) LB_KIND="$2"; shift 2 ;;
			--lb=*) LB_KIND="${1#--lb=}"; shift ;;
			-h|--help) sed -n '2,/^$/p' "${BASH_SOURCE[0]}" >&2; exit 0 ;;
			*) N="$1"; shift ;;
		esac
	done
	case "$LB_KIND" in
		node|nginx) ;;
		*) err "invalid --lb '$LB_KIND' (expected: node | nginx)"; exit 2 ;;
	esac
	[[ -z "$N" ]] && N="${FLOCK_SIO_PROCESSES:-$DEFAULT_N}"
	mkdir -p "$STATE_DIR"
	ensure_adapter_dep || true

	# Tear down any prior scaled tier + free the LB port (and backend ports).
	if [[ -f "$LB_PID_FILE" ]]; then
		log "existing scaled tier present — restarting"
		cmd_stop >/dev/null 2>&1 || true
	fi
	free_port "$LB_PORT"
	# Explicit `while` loops with a numeric counter: $N may be passed as a
	# positional (e.g. `start 4`) — keeping it out of arithmetic-as-variable
	# surprises on older bash. $i/$port/$blog/$bpid_file/$pid are local here.
	local i=0 port blog bpid_file pid
	while [[ $i -lt $N ]]; do free_port $((BASE_PORT + i)); i=$((i + 1)); done

	log "starting $N socketio backend(s) on :$BASE_PORT..$((BASE_PORT + N - 1))"
	: > "$BACKENDS_FILE"
	i=0
	while [[ $i -lt $N ]]; do
		port=$((BASE_PORT + i))
		blog="$STATE_DIR/backend-$i.log"
		bpid_file="$STATE_DIR/backend-$i.pid"
		# node apps/frappe/socketio.js resolves the bench root itself; the env var
		# overrides the listen port (node_utils.get_conf reads FRAPPE_SOCKETIO_PORT).
		FRAPPE_SOCKETIO_PORT="$port" FRAPPE_BENCH_ROOT="$BENCH" \
			node "$SOCKETIO_JS" >"$blog" 2>&1 &
		pid=$!
		echo "$pid" > "$bpid_file"
		echo "127.0.0.1:$port" >> "$BACKENDS_FILE"
		log "  backend[$i] pid=$pid :$port (log: $blog)"
		i=$((i + 1))
	done

	# Gate on backends listening before the LB tries to forward to them.
	i=0
	while [[ $i -lt $N ]]; do
		port=$((BASE_PORT + i))
		if ! wait_for_port "$port"; then
			err "backend[$i] on :$port did not listen in time — check $STATE_DIR/backend-$i.log"
			cmd_stop >/dev/null 2>&1 || true
			exit 1
		fi
		i=$((i + 1))
	done
	log "all $N backends listening"

	if [[ "$LB_KIND" == "nginx" ]]; then
		start_nginx_lb
	else
		start_node_lb
	fi

	echo
	log "scaled socketio tier is UP:"
	log "  LB:          ws://flock_os.localhost:$LB_PORT  (kind: $LB_KIND; k6 default WS_BASE_URL — no override needed)"
	log "  backends:    $N  (:$BASE_PORT..$((BASE_PORT + N - 1)))"
	if [[ -n "${FLOCK_SIO_ADAPTER_REDIS:-}" ]]; then
		log "  adapter:     @socket.io/redis-adapter -> DEDICATED \$FLOCK_SIO_ADAPTER_REDIS (${FLOCK_SIO_ADAPTER_REDIS})"
	else
		log "  adapter:     @socket.io/redis-adapter -> shared redis_socketio (set FLOCK_SIO_ADAPTER_REDIS for a dedicated instance)"
	fi
	log "run the §8 gate:"
	log "  k6 run -e WS_VUS=15000 -e WS_DURATION_SEC=120 load/ws_event_room.js"
	log "teardown:"
	log "  $PROG stop"
}

cmd_stop() {
	local killed=0
	# LB first so no new connections forward during backend teardown. The kind is
	# recorded at start time ($STATE_DIR/lb-kind): nginx tears down via its own
	# pidfile (SIGTERM to the master); node via the LB_PID_FILE we wrote.
	local lb_kind="node"
	[[ -f "$STATE_DIR/lb-kind" ]] && lb_kind="$(cat "$STATE_DIR/lb-kind" 2>/dev/null || echo node)"
	if [[ "$lb_kind" == "nginx" ]]; then
		local ngx_pid="$STATE_DIR/nginx.pid"
		if [[ -f "$ngx_pid" ]] && kill -0 "$(cat "$ngx_pid")" 2>/dev/null; then
			kill "$(cat "$ngx_pid")" 2>/dev/null || true
			killed=1
			for _ in 1 2 3 4 5 6 7 8 9 10; do
				{ kill -0 "$(cat "$ngx_pid")" 2>/dev/null || break; } && sleep 0.3
			done
		fi
		# Belt-and-braces: ask nginx to stop if the pidfile kill did not take.
		if [[ -f "$STATE_DIR/nginx.conf" ]]; then
			nginx -c "$STATE_DIR/nginx.conf" -p "$STATE_DIR/" -s stop >/dev/null 2>&1 || true
		fi
		rm -f "$ngx_pid"
	else
		if [[ -f "$LB_PID_FILE" ]] && kill -0 "$(cat "$LB_PID_FILE")" 2>/dev/null; then
			kill "$(cat "$LB_PID_FILE")" 2>/dev/null || true
			killed=1
		fi
		rm -f "$LB_PID_FILE"
	fi
	rm -f "$STATE_DIR/lb-kind"
	# Backends.
	if [[ -f "$BACKENDS_FILE" ]]; then
		local i=0
		while IFS= read -r _; do
			local bpid_file="$STATE_DIR/backend-$i.pid"
			if [[ -f "$bpid_file" ]] && kill -0 "$(cat "$bpid_file")" 2>/dev/null; then
				kill "$(cat "$bpid_file")" 2>/dev/null || true
				killed=1
			fi
			rm -f "$bpid_file"
			i=$((i + 1))
		done < "$BACKENDS_FILE"
	fi
	if [[ $killed -eq 1 ]]; then
		log "scaled socketio tier stopped."
		log "to restore the single-process socketio: bench restart  (or 'bench start' / Procfile 'socketio')"
	else
		log "no scaled tier was running."
	fi
}

cmd_status() {
	echo "scaled-socketio tier (state dir: $STATE_DIR)"
	echo "------------------------------------------------"
	local lb_kind="node"
	[[ -f "$STATE_DIR/lb-kind" ]] && lb_kind="$(cat "$STATE_DIR/lb-kind" 2>/dev/null || echo node)"
	local lb_pid_file="$LB_PID_FILE"
	[[ "$lb_kind" == "nginx" ]] && lb_pid_file="$STATE_DIR/nginx.pid"
	if [[ -f "$lb_pid_file" ]] && kill -0 "$(cat "$lb_pid_file")" 2>/dev/null; then
		echo "LB    kind=$lb_kind pid=$(cat "$lb_pid_file") :$LB_PORT  UP"
	else
		echo "LB    kind=$lb_kind :$LB_PORT  DOWN"
	fi
	if [[ -n "${FLOCK_SIO_ADAPTER_REDIS:-}" ]]; then
		echo "adapter dedicated: $FLOCK_SIO_ADAPTER_REDIS"
	else
		echo "adapter: shared redis_socketio (no FLOCK_SIO_ADAPTER_REDIS)"
	fi
	if [[ -f "$BACKENDS_FILE" ]]; then
		local i=0
		while IFS= read -r hp; do
			local bpid_file="$STATE_DIR/backend-$i.pid"
			local state=DOWN
			if [[ -f "$bpid_file" ]] && kill -0 "$(cat "$bpid_file")" 2>/dev/null; then state=UP; fi
			echo "backend[$i] $hp  $state"
			i=$((i + 1))
		done < "$BACKENDS_FILE"
	else
		echo "(no backends recorded — tier not started)"
	fi
	if [[ -f "$LB_LOG" ]]; then
		echo
		echo "LB connection distribution (tail of $LB_LOG):"
		grep 'socketio-lb stats:' "$LB_LOG" | tail -3 || true
	fi
}

usage() {
	sed -n '2,/^$/p' "${BASH_SOURCE[0]}" >&2
}

main() {
	local cmd="${1:-}"
	[[ $# -gt 0 ]] && shift || true
	case "$cmd" in
		start) cmd_start "$@" ;;
		stop) cmd_stop ;;
		restart) cmd_stop >/dev/null 2>&1 || true; cmd_start "$@" ;;
		status) cmd_status ;;
		""|-h|--help|help) usage ;;
		*) err "unknown command '$cmd'"; exit 2 ;;
	esac
}

main "$@"
