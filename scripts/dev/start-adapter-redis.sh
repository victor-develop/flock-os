#!/usr/bin/env bash
#
# Bring up a DEDICATED Redis for the @socket.io/redis-adapter (FLO-127 / FLO-10 §8).
#
# Why a dedicated adapter Redis: this bench's `redis_socketio` == `redis_cache`
# (`127.0.0.1:13000`). Under the §8 15k burst the scaled socketio tier drives 8x
# adapter pub/sub + Frappe cache/queue traffic through that ONE instance, so a
# Redis client emits `ETIMEDOUT` (the runbook's local-testbed ceiling #2 — NOT the
# connection-setup wall, which the tier already cleared in FLO-121). Giving the
# adapter its own Redis removes that contention. The adapter wiring
# (`wire-socketio-redis-adapter.sh`) honors `FLOCK_SIO_ADAPTER_REDIS` (a raw
# `redis://` URL) via `resolveAdapterClients`; this helper starts the instance and
# prints the URL to export.
#
# This is the dev-Mac / staging unblock. In production the adapter Redis is a
# managed/clustered Redis (the D3 escape hatch) — same env var, real instance.
#
# Usage:
#   scripts/dev/start-adapter-redis.sh start [port]   # bring up a dedicated Redis (default port 13010)
#   scripts/dev/start-adapter-redis.sh stop           # tear it down
#   scripts/dev/start-adapter-redis.sh restart [port] # stop + start
#   scripts/dev/start-adapter-redis.sh status         # up/down + PING
#
# Env:
#   FLOCK_SIO_ADAPTER_PORT   listen port (default 13010; positional [port] overrides)
#   FLOCK_SIO_BENCH          bench root (default: same resolution as the wire scripts)
#
# Then export the URL so every socketio backend resolves the adapter to it:
#   export FLOCK_SIO_ADAPTER_REDIS="redis://127.0.0.1:13010"
#   scripts/dev/scale-socketio.sh start
#
# Runbook: docs/development/ws-broadcast-delivery.md -> Prod-equivalent tier (FLO-127).
set -euo pipefail

PROG="$(basename "${BASH_SOURCE[0]}")"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

if [[ -n "${FLOCK_SIO_BENCH:-}" ]]; then
	BENCH="$FLOCK_SIO_BENCH"
else
	BENCH="${FRAPPE_BENCH_ROOT:-$REPO_ROOT/../flock-os-bench}"
fi

STATE_DIR="$BENCH/logs/redis-adapter"
PID_FILE="$STATE_DIR/redis.pid"
LOG="$STATE_DIR/redis.log"

DEFAULT_PORT="${FLOCK_SIO_ADAPTER_PORT:-13010}"

log() { echo "$PROG: $*"; }
err() { echo "$PROG: $*" >&2; }

ping_ok() {
	local port="$1"
	redis-cli -h 127.0.0.1 -p "$port" ping >/dev/null 2>&1
}

wait_for_port() {
	local port="$1" i
	for ((i = 0; i < 40; i++)); do
		if ping_ok "$port"; then return 0; fi
		sleep 0.25
	done
	return 1
}

resolve_port() {
	local p="${1:-$DEFAULT_PORT}"
	if ! [[ "$p" =~ ^[0-9]+$ ]] || (( p < 1 || p > 65535 )); then
		err "invalid port '$p' (expected 1..65535)"
		exit 2
	fi
	echo "$p"
}

cmd_start() {
	local port
	port="$(resolve_port "${1:-}")"
	mkdir -p "$STATE_DIR"

	if ping_ok "$port"; then
		log "dedicated adapter Redis already UP on :$port — nothing to do."
		print_url "$port"
		return 0
	fi

	command -v redis-server >/dev/null 2>&1 || { err "redis-server not found (brew install redis)."; exit 1; }

	# daemonize yes: redis forks, writes the pidfile, and returns immediately. The
	# instance is in-memory only (save "" / appendonly no) — adapter pub/sub state
	# is ephemeral and rebuilds on a tier restart, so no persistence is wanted.
	log "starting dedicated adapter Redis on :$port (in-memory, no persistence)"
	redis-server \
		--port "$port" \
		--bind 127.0.0.1 \
		--daemonize yes \
		--pidfile "$PID_FILE" \
		--logfile "$LOG" \
		--dir "$STATE_DIR" \
		--dbfilename adapter-dump.rdb \
		--save "" \
		--appendonly no >/dev/null 2>&1

	if ! wait_for_port "$port"; then
		err "dedicated Redis did not answer PING on :$port in time — check $LOG"
		exit 1
	fi

	log "dedicated adapter Redis is UP (pid=$(cat "$PID_FILE" 2>/dev/null || echo "?"))"
	print_url "$port"
}

print_url() {
	local port="$1"
	local url="redis://127.0.0.1:$port"
	echo
	log "export on EVERY shell that runs a socketio backend, then start the tier:"
	echo "    export FLOCK_SIO_ADAPTER_REDIS=\"$url\""
	echo "    scripts/dev/scale-socketio.sh start"
	echo "    # verify the adapter bound the dedicated instance: redis-cli -p $port ping"
}

read_pid() {
	[[ -f "$PID_FILE" ]] || { echo ""; return 1; }
	local p
	p="$(cat "$PID_FILE" 2>/dev/null || true)"
	[[ -n "$p" ]] || { echo ""; return 1; }
	echo "$p"
}

cmd_stop() {
	local port="${1:-$DEFAULT_PORT}"
	local killed=0
	local pid
	pid="$(read_pid || true)"
	if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
		kill "$pid" 2>/dev/null || true
		killed=1
		for _ in 1 2 3 4 5 6 7 8 9 10; do
			{ kill -0 "$pid" 2>/dev/null || break; } && sleep 0.3
		done
	fi
	rm -f "$PID_FILE"
	# Best-effort: also drop the listener if a stale redis-server still holds it.
	if ping_ok "$port"; then
		redis-cli -h 127.0.0.1 -p "$port" shutdown nosave >/dev/null 2>&1 || true
		killed=1
	fi
	if [[ $killed -eq 1 ]]; then
		log "dedicated adapter Redis stopped (port :$port)."
	else
		log "no dedicated adapter Redis was running on :$port."
	fi
}

cmd_status() {
	local port="${1:-$DEFAULT_PORT}"
	echo "dedicated adapter Redis (state dir: $STATE_DIR)"
	echo "------------------------------------------------"
	if ping_ok "$port"; then
		echo "  :$port  UP  ($(redis-cli -h 127.0.0.1 -p "$port" ping 2>/dev/null))"
		[[ -f "$PID_FILE" ]] && echo "  pid: $(cat "$PID_FILE" 2>/dev/null || echo '?')"
		echo "  URL: redis://127.0.0.1:$port"
		echo "  set on backends via: export FLOCK_SIO_ADAPTER_REDIS=\"redis://127.0.0.1:$port\""
	else
		echo "  :$port  DOWN"
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
		stop) cmd_stop "$@" ;;
		restart) cmd_stop "$@" >/dev/null 2>&1 || true; cmd_start "$@" ;;
		status) cmd_status "$@" ;;
		""|-h|--help|help) usage ;;
		*) err "unknown command '$cmd'"; exit 2 ;;
	esac
}

main "$@"
