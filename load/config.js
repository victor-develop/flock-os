// Flock OS — k6 smoke config (FLO-49 / FLO-10 ADR §8).
//
// Single env-driven config surface for the k6 smoke harness. Every knob is
// overridable via `k6 run -e KEY=VAL ...` so the same scripts run scaled-down
// locally and at the full 15k acceptance bar in CI. See load/README.md.

const DEFAULT_BASE_URL = "http://flock_os.localhost:8000";

// -- Bulk-attendance write smoke (bulk_attendance.js) -----------------------
// Target = 200 writes/sec x 150s ~= 30k attendance records (2x the 15k worst
// case, per FLO-10 §8 acceptance). Each k6 iteration submits one bulk batch of
// BATCH_ITEMS records, so the arrival rate is derived: rate = WRITES_PER_SEC /
// BATCH_ITEMS. BATCH_ITEMS is capped by the transport BULK_BATCH_SIZE=500
// (flock_os.reporting).
export const write = {
	baseUrl: __ENV.BASE_URL || DEFAULT_BASE_URL,
	// FLO-10 §3.2 REST contract: POST /api/method/flock_os.attendance.bulk_submit
	endpoint: __ENV.BULK_ENDPOINT || "/api/method/flock_os.attendance.bulk_submit",
	// The Flock Gathering id + the leader/branch-admin's resolved Flock Branch.
	// Seeded by the runtime (load/README.md -> Runtime fixtures).
	eventId: __ENV.EVENT_ID || "gathering-smoke",
	branchId: __ENV.BRANCH_ID || "branch-smoke",
	// Auth: a Frappe user (leader) with a single Flock Branch User Permission.
	username: __ENV.FLOCK_USER || "leader@flock.os",
	password: __ENV.FLOCK_PASSWORD || "flock",
	writesPerSec: parseInt(__ENV.WRITES_PER_SEC || "200", 10),
	durationSec: parseInt(__ENV.DURATION_SEC || "150", 10),
	// Records per bulk batch (<= 500, flock_os.reporting.BULK_BATCH_SIZE).
	batchItems: parseInt(__ENV.BATCH_ITEMS || "50", 10),
	// Stage seconds for the ramp-up (ramping-arrival-rate preAllocatedVUs).
	rampUpSec: parseInt(__ENV.RAMP_UP_SEC || "20", 10),
	// FLO-10 §8 acceptance thresholds.
	p95Millis: parseInt(__ENV.P95_MILLIS || "500", 10),
};

// -- Websocket room smoke (ws_event_room.js) --------------------------------
// 15k concurrent ws clients, each joining exactly one presence shard + the
// shared broadcast room for the event (FLO-10 §5.1).
export const ws = {
	baseUrl: (__ENV.WS_BASE_URL || "ws://flock_os.localhost:8000").replace(/^http/, "ws"),
	socketioPath: __ENV.SOCKETIO_PATH || "/socket.io",
	eventId: __ENV.EVENT_ID || write.eventId,
	// Default 10 shards == DEFAULT_SHARD_COUNT in flock_os.realtime.
	shardCount: parseInt(__ENV.SHARD_COUNT || "10", 10),
	vus: parseInt(__ENV.WS_VUS || "15000", 10),
	durationSec: parseInt(__ENV.WS_DURATION_SEC || "120", 10),
	// A broadcast is "seen" within this many ms (FLO-10 §8: ws broadcast < 1s).
	broadcastBudgetMillis: parseInt(__ENV.BROADCAST_BUDGET_MILLIS || "1000", 10),
};

// Compose the full bulk_submit URL.
export function bulkUrl() {
	return write.baseUrl + write.endpoint;
}
