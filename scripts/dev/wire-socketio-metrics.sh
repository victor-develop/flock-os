#!/usr/bin/env bash
#
# Wire the flock_os per-worker Prometheus /metrics surface into the Frappe v15
# realtime server (FLO-922 / FLO-586 §6 gap G1).
#
# The node socketio worker (`apps/frappe/socketio.js`) loads
# `apps/frappe/realtime/index.js` which constructs the socket.io `io` instance
# but has no per-app hook for standing up a Prometheus endpoint. This script
# inserts ONE idempotent, marker-guarded `require(...)` block that calls
# `attachMetrics(io)` AFTER `io` is built (post redis-adapter wiring, pre
# `server.listen`). The metrics LOGIC stays in flock_os
# (`realtime/metrics/flock_prometheus.js`); only a single guarded require lands
# in vendored Frappe. Same shape as the auth-cache / room-handler /
# redis-adapter wirings (independent anchor line, composable with all three).
#
# What this enables: the four critical §8 WS-SLO alerts (WSConnectSLOBreach,
# WSBroadcastSLOBreach, WSErrorCounterNonZero, WSessionsDropped — design §3.4)
# and the cluster-shape panels (per-worker clientsCount, rooms, connect/
# disconnect rate). Without this scrape source those alerts have no metric to
# arm against (FLO-897 alerting.md §1 + §5.3).
#
# Auto-wired: flock_os's `after_migrate`/`after_install` hooks re-run this
# (see flock_os/utils/realtime_setup.py), so a `bench update` (which rewrites
# index.js and would drop the wiring) re-applies it automatically — no manual
# runbook step, no silent regression. It stays idempotent + reversible.
#
# Runbook: docs/operations/production-instrumentation.md (FLO-922).
#
# Usage:
#   scripts/dev/wire-socketio-metrics.sh              # wire (idempotent)
#   scripts/dev/wire-socketio-metrics.sh --check      # exit 0 wired / 1 absent
#   scripts/dev/wire-socketio-metrics.sh --revert     # remove the wiring
#   scripts/dev/wire-socketio-metrics.sh --bench /path/to/bench
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

MARK_START="FLOCK_OS_REALTIME_METRICS_START"
MARK_END="FLOCK_OS_REALTIME_METRICS_END"

# Anchor: the Frappe v15 line that registers the namespace-connection handler.
# This sits AFTER `io` is constructed and AFTER the redis-adapter wiring block
# (which `io.adapter(...)` is wired into), so by the time we attachMetrics(io)
# the adapter + auth-cache + room-handler are all in place. Independent anchor
# from the other three wirings, so all four compose without collisions.
ANCHOR='realtime.on("connection", on_connection);'

# --check: non-mutating assert. Exits 0 if the wiring marker is present, 1 if
# absent. Use it in CI gates / runbooks to turn a dropped scrape endpoint into
# a loud failure instead of a silent WS-SLO blind spot (FLO-922 AC#1).
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
		# Delete the managed block (start..end, inclusive) — no pristine line to
		# restore because the wiring is inserted BEFORE its anchor (not a
		# replacement like the auth-cache swap).
		awk -v s="$MARK_START" -v e="$MARK_END" '
			$0 ~ s { skip=1; next }
			$0 ~ e { skip=0; next }
			!skip { print }
		' "$INDEX" > "$INDEX.tmp" && mv "$INDEX.tmp" "$INDEX"
		echo "$PROG: removed flock_os per-worker metrics wiring from $INDEX"
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

if ! grep -qF "$ANCHOR" "$INDEX"; then
	echo "$PROG: anchor '$ANCHOR' not found in $INDEX" >&2
	echo "$PROG: this Frappe version may differ — inspect $INDEX manually." >&2
	exit 1
fi

# Relative require path from apps/frappe/realtime/ -> apps/flock_os/realtime/metrics/.
# (realtime -> frappe -> apps, then flock_os/...). Kept relative so it is
# portable, identical in shape to the other three flock_os realtime wirings.
METRICS_REL="../../flock_os/realtime/metrics/flock_prometheus"

# Insert the guarded require immediately BEFORE the anchor line, so attachMetrics
# runs after the redis-adapter wiring has wired `io.adapter(...)` (the rooms
# gauge reads `io.sockets.adapter.rooms`) and before `server.listen` fires.
cp "$INDEX" "$INDEX.bak"
awk -v s="$MARK_START" -v e="$MARK_END" -v m="$METRICS_REL" -v anchor="$ANCHOR" '
	$0 == anchor {
		print "// " s " (managed by scripts/dev/wire-socketio-metrics.sh — do not edit)"
		print "try { require(\"" m "\").attachMetrics(io); } catch (err) { console.error(\"flock_os realtime metrics:\", (err && err.message) || err); }"
		print "// " e
	}
	{ print }
' "$INDEX" > "$INDEX.tmp" && mv "$INDEX.tmp" "$INDEX"

# Verify the insertion landed (marker present + anchor still intact).
if ! grep -q "$MARK_START" "$INDEX" || ! grep -qF "$ANCHOR" "$INDEX"; then
	cp "$INDEX.bak" "$INDEX"
	echo "$PROG: insertion did not land cleanly; original restored from .bak." >&2
	echo "$PROG: inspect $INDEX manually." >&2
	exit 1
fi
rm -f "$INDEX.bak"

echo "$PROG: wired flock_os per-worker /metrics surface into $INDEX"
echo "$PROG: scrape URL per worker: http://<worker-host>:9100/metrics"
echo "$PROG: restart the socketio process to apply: bench restart (or re-run 'bench start')."
