#!/usr/bin/env bash
#
# Wire the flock_os Redis adapter into the Frappe v15 realtime server (FLO-121).
#
# The §8 15k WS wall is a *single-process node socketio* connection-setup wall:
# one node event loop serializes 15k concurrent handshakes (TCP + engine.io
# upgrade + SIO CONNECT + per-room JOIN), so connect p95 balloons and <1 % of
# sessions establish. The auth cache (FLO-116) cleared the *auth-callback* wall;
# this wiring arms the fix for the *connection-setup* wall — scaling the socketio
# tier horizontally (N node processes behind a WS-aware LB, see
# scripts/dev/scale-socketio.sh) and attaching `@socket.io/redis-adapter` so the
# cluster behaves as one logical io instance across workers.
#
# What it inserts: ONE marker-guarded block into the bench's vendored
# `apps/frappe/realtime/index.js`, right before
# `realtime.on("connection", on_connection);`. The block sets the adapter on the
# `io` SERVER instance (socket.io v4: `.adapter(fn)` is a Server method that
# applies to ALL namespaces, including the per-site parent) using two node-redis
# clients created from
# the bench's `redis_socketio` URL via frappe's own `get_redis_subscriber`. The
# adapter LOGIC + opts live in flock_os
# (`realtime/adapters/flock_redis_adapter.js`); only this guarded block lands in
# vendored Frappe.
#
# Why a patch and not a framework hook: `@socket.io/redis-adapter` is set on the
# live `io` Server instance, which only exists inside vendored
# `index.js`. There is no Frappe extension point for it, so — exactly like the
# room-join handler (FLO-107) and the auth cache (FLO-116) — the least-invasive
# flock_os-owned option is a single guarded block, composed independently with
# the other two wirings (different anchor line).
#
# Auto-wired: flock_os's `after_migrate` / `after_install` hooks re-run this
# script (see `flock_os/utils/realtime_setup.py`), so a `bench update` (which
# rewrites index.js) re-inserts the block automatically — no manual runbook step,
# no silent regression. It stays idempotent + reversible if you drive it by hand.
# Runbook: docs/development/ws-broadcast-delivery.md -> Scaling the socketio tier.
#
# Prereq: `@socket.io/redis-adapter` must be installed in the flock_os app root
# (the repo root) — `scripts/dev/scale-socketio.sh` ensures this; a manual run is
# `npm install` from the repo root. Without the package the wiring is still
# inserted (armed) but the runtime try/catch logs the missing-package error; the
# cluster then fans out via the existing "events" pub/sub path only (defensive,
# not the full multi-process story).
#
# Usage:
#   scripts/dev/wire-socketio-redis-adapter.sh              # wire (idempotent)
#   scripts/dev/wire-socketio-redis-adapter.sh --check      # exit 0 wired / 1 absent (no change)
#   scripts/dev/wire-socketio-redis-adapter.sh --revert     # remove the wiring
#   scripts/dev/wire-socketio-redis-adapter.sh --bench /path/to/bench
#
# Then restart the socketio process so it reloads index.js:
#   bench restart   (or kill the `node .../socketio.js` pid; `bench start` respawns)
set -euo pipefail

PROG="$(basename "${BASH_SOURCE[0]}")"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

BENCH=""
REVERT=0
CHECK=0
while [[ $# -gt 0 ]]; do
	case "$1" in
		--revert) REVERT=1; shift ;;
		--check) CHECK=1; shift ;;
		--bench) BENCH="$2"; shift 2 ;;
		--bench=*) BENCH="${1#--bench=}"; shift ;;
		-h|--help)
			sed -n '2,/^$/p' "${BASH_SOURCE[0]}" >&2; exit 0 ;;
		*) echo "$PROG: unknown arg: $1" >&2; exit 2 ;;
	esac
done

# Resolve the bench dir: explicit flag > $FRAPPE_BENCH_ROOT > sibling flock-os-bench.
if [[ -z "$BENCH" ]]; then
	BENCH="${FRAPPE_BENCH_ROOT:-$REPO_ROOT/../flock-os-bench}"
fi
INDEX="$BENCH/apps/frappe/realtime/index.js"
if [[ ! -f "$INDEX" ]]; then
	echo "$PROG: realtime index not found at: $INDEX" >&2
	echo "$PROG: pass --bench <path> or set FRAPPE_BENCH_ROOT" >&2
	exit 1
fi

MARK_START="FLOCK_OS_REALTIME_REDIS_ADAPTER_START"
MARK_END="FLOCK_OS_REALTIME_REDIS_ADAPTER_END"

# The anchor the block is inserted before. Stable in Frappe v15's index.js (the
# per-site namespace is connected to on_connection here); independent of the
# join-handler anchor (inside on_connection) and the auth-cache anchor
# (realtime.use(authenticate);), so all three wirings compose.
ANCHOR='realtime.on("connection", on_connection);'

# --check: non-mutating assert. Exits 0 if the wiring marker is present, 1 if
# absent. Use it in CI gates / runbooks to turn a dropped adapter into a loud
# failure instead of a silent multi-process regression (FLO-121).
if [[ "$CHECK" -eq 1 ]]; then
	if [[ "$REVERT" -eq 1 ]]; then
		echo "$PROG: --check and --revert are mutually exclusive" >&2
		exit 2
	fi
	if grep -q "$MARK_START" "$INDEX"; then
		echo "$PROG: wired ($INDEX)."
		exit 0
	fi
	echo "$PROG: NOT wired — marker '$MARK_START' absent from $INDEX" >&2
	exit 1
fi

if grep -q "$MARK_START" "$INDEX"; then
	if [[ "$REVERT" -eq 1 ]]; then
		# Delete the managed block (start..end, inclusive). INSERT semantics: there
		# is no pristine line to restore (the anchor stays), so revert = removal.
		awk -v s="$MARK_START" -v e="$MARK_END" '
			$0 ~ s { skip=1; next }
			$0 ~ e { skip=0; next }
			!skip { print }
		' "$INDEX" > "$INDEX.tmp" && mv "$INDEX.tmp" "$INDEX"
		echo "$PROG: removed flock_os redis-adapter wiring from $INDEX"
		echo "$PROG: restart the socketio process to apply (bench restart)."
	else
		echo "$PROG: already wired ($INDEX). Nothing to do."
	fi
	exit 0
fi

if [[ "$REVERT" -eq 1 ]]; then
	echo "$PROG: nothing to revert (no wiring marker in $INDEX)."
	exit 0
fi

# Relative require path from apps/frappe/realtime/ -> apps/flock_os/realtime/adapters/.
# (realtime -> frappe -> apps, then flock_os/...). Kept relative so it is portable,
# and identical in shape to the join-handler + auth-cache wirings' relative requires.
ADAPTER_REL="../../flock_os/realtime/adapters/flock_redis_adapter"

# Insert the guarded block BEFORE the anchor (so the adapter is set before
# connections arrive). `get_redis_subscriber` is already in scope in index.js
# (destructured from ../node_utils at the top of the file); the block creates +
# connects two node-redis clients off the bench's redis_socketio URL and sets the
# adapter on the `io` SERVER instance (socket.io v4: `.adapter(fn)` is a Server
# method that applies the adapter to ALL namespaces, including the per-site
# `io.of(/^\/.*$/)` parent + its dynamic child namespaces — calling it on the
# Namespace would fail with "realtime.adapter is not a function"). The pub/sub
# clients get `on("error")` handlers so a Redis blip under load (e.g. ETIMEDOUT)
# is LOGGED instead of crashing the worker (an unhandled 'error' event kills the
# node process — proven under the 15k burst). Clients are connected
# fire-and-forget (socket.io-redis-adapter tolerates a not-yet-connected client;
# the connect promise just ensures pub/sub is live). On ANY error (e.g. the npm
# package missing) the try/catch logs and the server keeps booting.
cp "$INDEX" "$INDEX.bak"
awk -v s="$MARK_START" -v e="$MARK_END" -v a="$ADAPTER_REL" '
	/realtime\.on\("connection", on_connection\);/ {
		print "// " s " (managed by scripts/dev/wire-socketio-redis-adapter.sh — do not edit)"
		print "try {"
		print "\tconst { createRedisAdapter } = require(\"" a "\");"
		print "\tconst _flockAdapterPub = get_redis_subscriber(\"redis_socketio\");"
		print "\tconst _flockAdapterSub = _flockAdapterPub.duplicate();"
		print "\t_flockAdapterPub.on(\"error\", (e) => console.error(\"flock_os redis-adapter pub:\", (e && e.message) || e));"
		print "\t_flockAdapterSub.on(\"error\", (e) => console.error(\"flock_os redis-adapter sub:\", (e && e.message) || e));"
		print "\tPromise.all([_flockAdapterPub.connect(), _flockAdapterSub.connect()]).catch((err) => console.error(\"flock_os redis-adapter connect:\", (err && err.message) || err));"
		print "\tio.adapter(createRedisAdapter(_flockAdapterPub, _flockAdapterSub));"
		print "} catch (err) { console.error(\"flock_os realtime redis-adapter:\", (err && err.message) || err); }"
		print "// " e
	}
	{ print }
' "$INDEX" > "$INDEX.tmp" && mv "$INDEX.tmp" "$INDEX"

# The awk pattern above intentionally matches the actual Frappe line; verify it landed.
if ! grep -q "$MARK_START" "$INDEX"; then
	cp "$INDEX.bak" "$INDEX"
	echo "$PROG: insertion anchor '$ANCHOR' not found;" >&2
	echo "$PROG: this Frappe version may differ — inspect $INDEX manually." >&2
	echo "$PROG: original restored from .bak" >&2
	exit 1
fi
rm -f "$INDEX.bak"

echo "$PROG: wired flock_os redis-adapter into $INDEX"
echo "$PROG: restart the socketio process to apply: bench restart (or re-run 'bench start')."
echo "$PROG: to run the scaled tier (N processes + LB), see scripts/dev/scale-socketio.sh."
