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
//
// Frappe v15 realtime (`apps/frappe/realtime/index.js`) namespaces per site via
// `io.of(/^\/.*$/)` and authenticates each namespace with the `sid` cookie /
// `Authorization` header (realtime/middlewares/authenticate.js). So a client
// must CONNECT the `/<site>` Socket.IO namespace AND present a valid session
// (FLO-107 — previously the smoke hit the unauth default `/` namespace, which
// shares no rooms with the site namespace the projector publishes to).
export const ws = {
	// Socket.IO server port. The web (gunicorn) port 8000 404s /socket.io; the
	// realtime node server listens on 9000 (conf.socketio_port). Override for a
	// remote/tunneled bench.
	baseUrl: (__ENV.WS_BASE_URL || "ws://flock_os.localhost:9000").replace(/^http/, "ws"),
	socketioPath: __ENV.SOCKETIO_PATH || "/socket.io",
	// Frappe per-site realtime namespace. ``frappe.publish_realtime`` emits to
	// ``/<site>``; the client must CONNECT the same namespace to land in the
	// projector's room universe. Leading ``/`` is added by the smoke.
	site: __ENV.SITE || "flock_os.localhost",
	// HTTP origin the realtime auth middleware cross-checks against the Host
	// header (authenticate.js: get_hostname(host) == get_hostname(origin)) AND
	// uses as the base for its `get_user_info` callback (utils.js get_url =
	// origin + path). So this MUST point at the live web server — port and all
	// (default :8000); a port-less origin makes the node server call back to
	// :80 and reject the namespace with `Unauthorized: AggregateError` (FLO-107).
	origin: __ENV.WS_ORIGIN || write.baseUrl,
	eventId: __ENV.EVENT_ID || write.eventId,
	// Default 10 shards == DEFAULT_SHARD_COUNT in flock_os.realtime.
	shardCount: parseInt(__ENV.SHARD_COUNT || "10", 10),
	vus: parseInt(__ENV.WS_VUS || "15000", 10),
	durationSec: parseInt(__ENV.WS_DURATION_SEC || "120", 10),
	// Ramp-up duration to full VU count. A gentler ramp reduces connection-burst
	// establishment failures (k6 client-side) at the 15k bar — the default 60s is
	// the §8 spec ramp; increase via WS_RAMP_UP_SEC if the host can't sustain the
	// 250 VUs/s connect rate without dropping sockets.
	rampUpSec: parseInt(__ENV.WS_RAMP_UP_SEC || "60", 10),
	// A broadcast is "seen" within this many ms (FLO-10 §8: ws broadcast < 1s).
	broadcastBudgetMillis: parseInt(__ENV.BROADCAST_BUDGET_MILLIS || "1000", 10),
	// Auth: a Frappe user (defaults to the bulk leader). The site namespace
	// requires a valid ``sid`` (validated via frappe.realtime.get_user_info).
	username: __ENV.FLOCK_USER || write.username,
	password: __ENV.FLOCK_PASSWORD || write.password,
};

// -- Registration load profile (event_registration.js) ----------------------
// Drives concurrent ``register_for_event`` calls (FLO-7 §5 public registration
// endpoint) at ramp rates up to the 15k-attendee burst. Each iteration registers
// one unique ``Flock Member`` (by PK) against the target gathering. The caller
// (the smoke leader) must hold branch scope over the gathering's branch so the
// on-behalf registration path (§5) admits the member. Replays hit the
// ``(gathering, registrant)`` UNIQUE index and return ``already_registered:
// true`` — the idempotency backstop the §8 gate quotes (FLO-669 #2).
//
// Member identity: each VU maps to a deterministic member PK
// ``${memberPrefix}-${memberOffset + __VU}`` so the 200-VU smoke registers 200
// distinct members and the full run registers up to 15k. The members must be
// pre-seeded on the gathering's branch (scale seed or a smoke-specific fixture).
export const registration = {
	baseUrl: __ENV.BASE_URL || DEFAULT_BASE_URL,
	// FLO-7 §8 REST contract: POST /api/method/<dotted-path>.register_for_event
	endpoint:
		__ENV.REGISTRATION_ENDPOINT ||
		"/api/method/flock_os.flock_os.doctype.flock_event_registration.flock_event_registration.register_for_event",
	// The approved Flock Gathering id (must be Approved + scope != None).
	eventId: __ENV.EVENT_ID || write.eventId,
	// Member PK prefix + offset so each VU maps to a unique, in-scope member.
	memberPrefix: __ENV.MEMBER_PREFIX || "scale-member",
	memberOffset: parseInt(__ENV.MEMBER_OFFSET || "0", 10),
	// Auth: the smoke leader (holds branch scope over the gathering's branch).
	username: __ENV.FLOCK_USER || write.username,
	password: __ENV.FLOCK_PASSWORD || write.password,
	// Leader registers on behalf (§5); the caller's branch scope is checked.
	registeredVia: __ENV.REGISTERED_VIA || "Leader",
	// Ramp to this many registrations/sec (15k over ~120s = ~125/s at full bar).
	registrationsPerSec: parseInt(__ENV.REGISTRATIONS_PER_SEC || "50", 10),
	durationSec: parseInt(__ENV.REG_DURATION_SEC || "120", 10),
	rampUpSec: parseInt(__ENV.REG_RAMP_UP_SEC || "20", 10),
	// §8 registration p95 target (registration is user-facing → 1s budget).
	p95Millis: parseInt(__ENV.REG_P95_MILLIS || "1000", 10),
};

// Compose the full bulk_submit URL.
export function bulkUrl() {
	return write.baseUrl + write.endpoint;
}

// Compose the full register_for_event URL.
export function registrationUrl() {
	return registration.baseUrl + registration.endpoint;
}
