#!/usr/bin/env bash
#
# Wire the flock_os per-connection auth cache into the Frappe v15 realtime server
# (FLO-116 / FLO-14 / FLO-10 §8).
#
# Frappe v15's site-namespace auth middleware
# (`apps/frappe/realtime/middlewares/authenticate.js`) fires ONE
# `GET /api/method/frappe.realtime.get_user_info` HTTP callback per WS connection.
# At the §8 15k bar every client shares one `sid`, so that is ~15k identical
# gunicorn round-trips in a burst -> the node server's superagent calls hit
# `ETIMEDOUT`, connections cycle/fail (connect p95 2.26 s), and broadcasts drop
# (receive_errors 8255). The fix is a flock_os-owned, sid-keyed cache
# (`realtime/middlewares/flock_auth_cache.js`) so get_user_info fires ONCE per
# session, not once per connection (1 call + 14,999 in-memory hits).
#
# socket.io's `namespace.use()` has no public removal API and APPENDS, so simply
# registering the cached middleware alongside the original would still let the
# original run first (firing the HTTP). Instead this script REPLACES the single
# `realtime.use(authenticate);` line in the vendored index.js with a guarded
# `realtime.use(<flock cache>.wrap(authenticate))`. The cache LOGIC stays in
# flock_os; only that one-line swap lands in vendored Frappe. It is
# marker-guarded, idempotent, and reversible — the same shape as
# wire-socketio-handler.sh, and independent of it (different anchor line), so
# the room-join handler wiring and this wiring compose.
#
# Auto-wired: flock_os's `after_migrate`/`after_install` hooks re-run this
# (see flock_os/utils/realtime_setup.py), so a `bench update` (which rewrites
# index.js and would drop the swap) re-applies it automatically — no manual
# runbook step, no silent regression. Runbook:
# docs/development/ws-broadcast-delivery.md -> auth cache.
#
# Usage:
#   scripts/dev/wire-socketio-auth-cache.sh              # wire (idempotent)
#   scripts/dev/wire-socketio-auth-cache.sh --check      # exit 0 wired / 1 absent (no change)
#   scripts/dev/wire-socketio-auth-cache.sh --revert     # restore the original line
#   scripts/dev/wire-socketio-auth-cache.sh --bench /path/to/bench
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

MARK_START="FLOCK_OS_REALTIME_AUTH_CACHE_START"
MARK_END="FLOCK_OS_REALTIME_AUTH_CACHE_END"

# The pristine Frappe line being replaced (kept here so --revert can restore it
# exactly, and so the wiring is self-describing). Note: this is a top-level
# statement in index.js — column 0, no indent — so the restored + inserted lines
# match the file's actual indentation (unlike the room-handler anchor, which
# lives inside on_connection and is tab-indented).
PRISTINE_LINE='realtime.use(authenticate);'

# --check: non-mutating assert. Exits 0 if the wiring marker is present, 1 if
# absent. Use it in CI gates / runbooks to turn a dropped cache into a loud
# failure instead of a silent §8 regression.
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
		# Remove the managed block (start..end, inclusive) and restore the
		# pristine line that the wiring originally replaced.
		awk -v s="$MARK_START" -v e="$MARK_END" -v restore="$PRISTINE_LINE" '
			$0 ~ s { print restore; skip=1; next }
			$0 ~ e { skip=0; next }
			!skip { print }
		' "$INDEX" > "$INDEX.tmp" && mv "$INDEX.tmp" "$INDEX"
		echo "$PROG: removed flock_os auth-cache wiring from $INDEX (restored realtime.use(authenticate);)"
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

if ! grep -q 'realtime\.use(authenticate);' "$INDEX"; then
	echo "$PROG: anchor 'realtime.use(authenticate);' not found in $INDEX" >&2
	echo "$PROG: this Frappe version may differ — inspect $INDEX manually." >&2
	exit 1
fi

# Relative require path from apps/frappe/realtime/ -> apps/flock_os/realtime/middlewares/.
# (realtime -> frappe -> apps, then flock_os/...). Kept relative so it is portable,
# and identical in shape to the room-handler wiring's relative require.
CACHE_REL="../../flock_os/realtime/middlewares/flock_auth_cache"

# Replace `realtime.use(authenticate);` with the marker-guarded cached swap.
# `authenticate` (the const already required above in index.js) is passed to
# `.wrap`, so the original middleware stays the single source of validation.
cp "$INDEX" "$INDEX.bak"
awk -v s="$MARK_START" -v e="$MARK_END" -v c="$CACHE_REL" '
	/realtime\.use\(authenticate\);/ {
		print "// " s " (managed by scripts/dev/wire-socketio-auth-cache.sh — do not edit)"
		print "realtime.use(require(\"" c "\").wrap(authenticate));"
		print "// " e
		next
	}
	{ print }
' "$INDEX" > "$INDEX.tmp" && mv "$INDEX.tmp" "$INDEX"

# Verify the swap landed (marker present) and the pristine line is gone.
if ! grep -q "$MARK_START" "$INDEX" || grep -q 'realtime\.use(authenticate);' "$INDEX"; then
	cp "$INDEX.bak" "$INDEX"
	echo "$PROG: replacement did not land cleanly; original restored from .bak." >&2
	echo "$PROG: inspect $INDEX manually." >&2
	exit 1
fi
rm -f "$INDEX.bak"

echo "$PROG: wired flock_os auth cache into $INDEX (replaced realtime.use(authenticate);)"
echo "$PROG: restart the socketio process to apply: bench restart (or re-run 'bench start')."
