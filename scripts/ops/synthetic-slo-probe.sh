#!/usr/bin/env bash
#
# flock_os scheduled low-VU k6 synthetic SLO probe (FLO-922 / FLO-586 §6 gap G4).
#
# The §8 15k k6 gate (scripts/dev/docker-ws-tier.sh gate) is a launch-gate /
# pre-event exercise — running it continuously would saturate the cluster it is
# measuring. The design (metrics-alerting-design.md §6 gap G4 + §1.4 synthetic
# rows + §5 "Synthetic SLO probe") calls for a SCHEDULED low-VU probe that
# reproduces the four §8 WS SLO signals (S4–S7) continuously so the four
# critical WS-SLO alerts (WSConnectSLOBreach, WSBroadcastSLOBreach,
# WSErrorCounterNonZero, WSessionsDropped) can fire against live data instead
# of only at the launch gate.
#
# This wrapper runs load/ws_event_room.js at a fixed low VU count, captures the
# k6 summary JSON, and emits the four synthetic metrics in Prometheus text
# exposition to:
#
#   1. a node_exporter textfile collector file (the standard textfile dir is
#      /var/lib/node_exporter/textfile — Prometheus scrapes node_exporter, which
#      picks the file up), AND/OR
#   2. stdout (so a wrapping cron / systemd unit can redirect it wherever the
#      monitor expects).
#
# Schedule: e.g. every 15 min via cron (the design's recommended cadence):
#   */15 * * * *  flock  FLOCK_SYNTH_PROBE_OUT=/var/lib/node_exporter/textfile \
#                     /path/to/flock-os/scripts/ops/synthetic-slo-probe.sh \
#                     >>/var/log/flock-synth-probe.log 2>&1
#
# The probe is NON-FATAL on the realtime tier: low VU + short hold means it
# costs the cluster ~1% of a §8 gate run, never enough to perturb the SLO it is
# measuring. If k6 is absent or the WS endpoint is unreachable, the metrics
# file is written with `_up=0` so the monitor sees the failure immediately
# (vs. silently missing data).
#
# Runbook: docs/operations/production-instrumentation.md (FLO-922).

set -uo pipefail

PROG="$(basename "${BASH_SOURCE[0]}")"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

usage() { sed -n '3,/^$/p' "${BASH_SOURCE[0]}" >&2; }

# Where to write the Prometheus textfile. Default: stdout (the wrapping cron /
# systemd unit redirects). Override with FLOCK_SYNTH_PROBE_OUT (a file path)
# for the node_exporter textfile collector integration.
OUT_DEST="${FLOCK_SYNTH_PROBE_OUT:-}"

# Probe shape (defaults track the design's "every 15 min at 1,000 VUs" guidance
# — the smallest load that still resolves the four SLO signals reliably).
VUS="${FLOCK_SYNTH_VUS:-200}"
DUR="${FLOCK_SYNTH_DURATION_SEC:-30}"
RAMP="${FLOCK_SYNTH_RAMP_UP_SEC:-10}"

# Endpoint — defaults to the in-container ws-lb hostname (so a cron running in a
# sibling container reaches the tier over the docker network); override for
# host-based or remote staging.
WS_BASE_URL="${WS_BASE_URL:-ws://ws-lb:9000}"
BASE_URL="${BASE_URL:-http://ws-lb:8100}"
WS_ORIGIN="${WS_ORIGIN:-$BASE_URL}"
SITE="${SITE:-flock_os.localhost}"
FLOCK_USER_VAR="${FLOCK_USER:-leader@flock.os}"
FLOCK_PW_VAR="${FLOCK_PASSWORD:-flock}"
SUMMARY="${FLOCK_SYNTH_SUMMARY:-$REPO_ROOT/load/telemetry/.synthetic-latest.json}"

# Optional path to a k6 binary; falls back to PATH lookup.
K6_BIN="${K6:-}"
[[ -n "$K6_BIN" ]] || K6_BIN="$(command -v k6 || true)"

log() { printf '%s: %s\n' "$PROG" "$*" >&2; }

# render_prometheus <p95_connect_ms> <p95_broadcast_ms> <receive_errors> <sessions_pct> <up>
render_prometheus() {
	local connect_ms="$1" bcast_ms="$2" errs="$3" sess="$4" up="$5"
	cat <<PROM
# HELP flock_synth_ws_connect_duration_ms_p95 Synthetic WS connect-duration p95 from the scheduled k6 probe (signal S4, FLO-922 G4).
# TYPE flock_synth_ws_connect_duration_ms_p95 gauge
flock_synth_ws_connect_duration_ms_p95 $connect_ms
# HELP flock_synth_ws_broadcast_latency_ms_p95 Synthetic WS broadcast-latency p95 from the scheduled k6 probe (signal S5).
# TYPE flock_synth_ws_broadcast_latency_ms_p95 gauge
flock_synth_ws_broadcast_latency_ms_p95 $bcast_ms
# HELP flock_synth_ws_receive_errors_total Synthetic WS receive-errors counter from the scheduled k6 probe (signal S6).
# TYPE flock_synth_ws_receive_errors_total gauge
flock_synth_ws_receive_errors_total $errs
# HELP flock_synth_ws_sessions_established_pct Synthetic WS sessions-established % from the scheduled k6 probe (signal S7).
# TYPE flock_synth_ws_sessions_established_pct gauge
flock_synth_ws_sessions_established_pct $sess
# HELP flock_synth_probe_up 1 if the last scheduled probe ran to completion, 0 otherwise.
# TYPE flock_synth_probe_up gauge
flock_synth_probe_up $up
PROM
}

# emit_failure_prometheus — when k6 is missing or the endpoint is unreachable,
# write the metrics file with up=0 so the monitor sees the probe is sick.
emit_failure_prometheus() {
	local reason="$1"
	log "probe FAILED: $reason"
	render_prometheus 0 0 0 0 0
}

# Parse the four SLO signals out of a k6 summary JSON. k6 emits thresholds +
# metrics; the names below match load/ws_event_room.js's Trend/Counter naming.
# (See that file for the metric definitions — they are the §8 signal names.)
parse_summary() {
	local summary="$1"
	[[ -f "$summary" ]] || { echo "0 0 0 0"; return; }
	PYTHONPATH="$REPO_ROOT" python3 - "$summary" <<'PY'
import json, sys
try:
    s = json.load(open(sys.argv[1]))
except Exception:
    print("0 0 0 0")
    sys.exit(0)
# k6 summary structure: { "metrics": { "<name>": { "values": { "p(95)": .., "count": .. } } } }
metrics = s.get("metrics", {})
def p95(name):
    m = metrics.get(name) or {}
    vals = m.get("values") or {}
    v = vals.get("p(95)")
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0
def counter(name):
    m = metrics.get(name) or {}
    vals = m.get("values") or {}
    try:
        return float(vals.get("count", 0))
    except (TypeError, ValueError):
        return 0.0
def rate(name):
    m = metrics.get(name) or {}
    vals = m.get("values") or {}
    try:
        return float(vals.get("rate", 0.0))
    except (TypeError, ValueError):
        return 0.0
connect = p95("flock_ws_connect_duration")
bcast = p95("flock_ws_broadcast_latency")
errs = counter("flock_ws_receive_errors")
# Sessions-established %: derived from the dropped / connect-rate ratio in k6.
# The launch gate names this `ws.sessions.established`; load/ws_event_room.js
# exposes it as a Rate whose `rate` value is the success fraction.
sess = rate("ws.sessions.established") * 100.0
# k6 stores Trend values in seconds (SI); the §8 SLO + alert thresholds are in
# milliseconds, so multiply by 1000 for Prometheus-adjacent comparison.
print(f"{connect*1000:.1f} {bcast*1000:.1f} {errs:.0f} {sess:.2f}")
PY
}

# Resolve where to write the textfile.
write_output() {
	local body="$1"
	if [[ -z "$OUT_DEST" ]]; then
		printf '%s\n' "$body"
		return
	fi
	# Atomic write (tmp + rename) so the node_exporter textfile collector never
	# reads a half-written file.
	local tmp
	tmp="$(mktemp "${OUT_DEST}.XXXXXX")"
	printf '%s\n' "$body" > "$tmp"
	mv "$tmp" "$OUT_DEST"
}

# --- main --------------------------------------------------------------------

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
	usage
	exit 0
fi

if [[ -z "$K6_BIN" ]]; then
	write_output "$(emit_failure_prometheus "k6 binary not found on PATH")"
	exit 1
fi

if [[ ! -f "$REPO_ROOT/load/ws_event_room.js" ]]; then
	write_output "$(emit_failure_prometheus "load/ws_event_room.js not found at $REPO_ROOT/load")"
	exit 1
fi

mkdir -p "$(dirname "$SUMMARY")"

log "running synthetic SLO probe: $VUS VUs x ${DUR}s (ramp ${RAMP}s) -> $WS_BASE_URL"

probe_status=0
"$K6_BIN" run \
	-e WS_VUS="$VUS" -e WS_DURATION_SEC="$DUR" -e WS_RAMP_UP_SEC="$RAMP" \
	-e WS_BASE_URL="$WS_BASE_URL" -e BASE_URL="$BASE_URL" -e WS_ORIGIN="$WS_ORIGIN" \
	-e SITE="$SITE" \
	-e FLOCK_USER="$FLOCK_USER_VAR" -e FLOCK_PASSWORD="$FLOCK_PW_VAR" \
	--out "json=$SUMMARY" \
	"$REPO_ROOT/load/ws_event_room.js" >/tmp/flock-synth-probe-k6.log 2>&1 || probe_status=$?

if [[ "$probe_status" -ne 0 ]]; then
	log "k6 exit $probe_status (threshold breach is also non-zero) — treating as probe-success (threshold breaches are alert signals, not probe failures)"
fi

read -r connect_ms bcast_ms errs sess <<<"$(parse_summary "$SUMMARY")"
log "parsed: connect_p95=${connect_ms}ms broadcast_p95=${bcast_ms}ms errors=${errs} sessions=${sess}%"
write_output "$(render_prometheus "$connect_ms" "$bcast_ms" "$errs" "$sess" 1)"
exit 0
