#!/usr/bin/env bash
#
# Wire the flock_os room-join handler into the Frappe v15 realtime server.
#
# Frappe v15 (`apps/frappe/realtime/index.js`) has no per-app socket handler
# loader, so flock_os's `join` handler must be registered from inside the
# server's `on_connection`. This script inserts ONE idempotent, guarded
# `require(...)` line into the bench's vendored `index.js`; the handler LOGIC
# stays in flock_os (`realtime/handlers/flock_room_handlers.js`). This is the
# runtime wiring the §5.1 sharded-room delivery path needs (FLO-107).
#
# Why a patch and not a framework hook: Frappe v15 ships no extension point for
# custom socket events (no `socketio_handler` app hook; index.js loads only
# `frappe_handlers`). The least-invasive flock_os-owned option is a single
# guarded require. Re-run this after a `bench update`/Frappe reinstall (it is
# idempotent). Runbook: docs/development/ws-broadcast-delivery.md.
#
# Usage:
#   scripts/dev/wire-socketio-handler.sh              # wire (idempotent)
#   scripts/dev/wire-socketio-handler.sh --revert     # remove the wiring
#   scripts/dev/wire-socketio-handler.sh --bench /path/to/bench
#
# Then restart the socketio process so it reloads index.js:
#   bench restart   (or kill the `node .../socketio.js` pid; `bench start` respawns)
set -euo pipefail

PROG="$(basename "${BASH_SOURCE[0]}")"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

BENCH=""
REVERT=0
while [[ $# -gt 0 ]]; do
	case "$1" in
		--revert) REVERT=1; shift ;;
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

MARK_START="FLOCK_OS_REALTIME_HANDLER_START"
MARK_END="FLOCK_OS_REALTIME_HANDLER_END"

if grep -q "$MARK_START" "$INDEX"; then
	if [[ "$REVERT" -eq 1 ]]; then
		# Delete the managed block (start..end, inclusive).
		awk -v s="$MARK_START" -v e="$MARK_END" '
			$0 ~ s { skip=1; next }
			$0 ~ e { skip=0; next }
			!skip { print }
		' "$INDEX" > "$INDEX.tmp" && mv "$INDEX.tmp" "$INDEX"
		echo "$PROG: removed flock_os realtime handler wiring from $INDEX"
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

# Relative require path from apps/frappe/realtime/ → apps/flock_os/realtime/handlers/.
# (realtime→frappe→apps, then flock_os/...). Kept relative so it is portable.
HANDLER_REL="../../flock_os/realtime/handlers/flock_room_handlers"

# Insert the guarded require immediately after `frappe_handlers(realtime, socket);`.
cp "$INDEX" "$INDEX.bak"
awk -v s="$MARK_START" -v e="$MARK_END" -v h="$HANDLER_REL" '
	/frappe_handlers\(realtime, socket\);/ {
		print
		print "\t// " s " (managed by scripts/dev/wire-socketio-handler.sh — do not edit)"
		print "\ttry { require(\"" h "\")(socket, { frappe_request: require(\"./utils\").frappe_request }); } catch (err) { console.error(\"flock_os realtime handler:\", (err && err.message) || err); }"
		print "\t// " e
		next
	}
	{ print }
' "$INDEX" > "$INDEX.tmp" && mv "$INDEX.tmp" "$INDEX"

# The awk pattern above intentionally matches the actual Frappe call; verify it landed.
if ! grep -q "$MARK_START" "$INDEX"; then
	cp "$INDEX.bak" "$INDEX"
	echo "$PROG: insertion anchor 'frappe_handlers(realtime, socket);' not found;" >&2
	echo "$PROG: this Frappe version may differ — inspect $INDEX manually." >&2
	echo "$PROG: original restored from .bak" >&2
	exit 1
fi
rm -f "$INDEX.bak"

echo "$PROG: wired flock_os room-join handler into $INDEX"
echo "$PROG: restart the socketio process to apply: bench restart (or re-run 'bench start')."
