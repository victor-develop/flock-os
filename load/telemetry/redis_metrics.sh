#!/usr/bin/env bash
# Flock OS – Redis runtime telemetry snapshot (FLO-49 / FLO-10 §8, D3/D5 triggers).
#
# Captures the three Redis signals that gate a 15k-attendee scale run:
#   * connected clients (+ pubsub-mode clients) — realtime fan-out pressure
#   * flock:* pub/sub channel count + per-room subscriber counts (shard balance)
#   * RQ queue depth — the flock_attendance bulk queue + the attendance_import_error
#     dead-letter queue (queue-drains-<60s acceptance, FLO-10 §3.3)
#
# Run it on a sample cadence during a k6 smoke to emit a time series, e.g.:
#   while k6 run ...; do ./load/telemetry/redis_metrics.sh; sleep 5; done
#
# Config via env (defaults match Homebrew Frappe dev): REDIS_URL, RQ_QUEUES.
set -euo pipefail

REDIS_URL="${REDIS_URL:-redis://localhost:6379}"
# Bulk attendance job queue + dead-letter queue (flock_os.reporting constants).
RQ_QUEUES="${RQ_QUEUES:-flock_attendance attendance_import_error default}"
TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

rds() { redis-cli -u "$REDIS_URL" "$@" 2>/dev/null; }

connected=$(rds INFO clients | awk -F: '/^connected_clients:/ {gsub(/\r/,"",$2); print $2}')
pubsub_clients=$(rds INFO clients | awk -F: '/^pubsub_clients:/ {gsub(/\r/,"",$2); print $2}')
# Flock realtime namespace channels (flock_os:event:* + flock:* pub/sub backbone).
flock_channels=$(rds PUBSUB CHANNELS 'flock*' | wc -l | tr -d ' ')

echo "# flock redis telemetry @ $TS"
echo "flock_redis_connected_clients ${connected:-0}"
echo "flock_redis_pubsub_clients ${pubsub_clients:-0}"
echo "flock_redis_pubsub_channels ${flock_channels:-0}"

# Per-queue RQ depth (rq stores jobs in rq:queue:<name> Redis lists).
for q in $RQ_QUEUES; do
	depth=$(rds LLEN "rq:queue:$q" | tr -d -c '0-9')
	echo "flock_rq_depth{queue=\"$q\"} ${depth:-0}"
done

# Subscriber counts for a sharded event room (pass EVENT_ID to check balance).
if [[ -n "${EVENT_ID:-}" ]]; then
	for shard in $(seq 0 9); do
		room="flock_os:event:${EVENT_ID}:shard:${shard}"
		n=$(rds PUBSUB NUMSUB "$room" | tail -1 | tr -d -c '0-9')
		echo "flock_rt_room_subscribers{room=\"shard:${shard}\"} ${n:-0}"
	done
	n=$(rds PUBSUB NUMSUB "flock_os:event:${EVENT_ID}:broadcast" | tail -1 | tr -d -c '0-9')
	echo "flock_rt_room_subscribers{room=\"broadcast\"} ${n:-0}"
fi
