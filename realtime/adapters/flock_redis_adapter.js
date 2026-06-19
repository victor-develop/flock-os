// flock_os realtime Redis adapter for the scaled socketio tier (FLO-121 /
// FLO-14 / FLO-10 §8).
//
// Background
// ----------
// The §8 15k WS wall (FLO-121) is a *single-process node socketio*
// connection-setup wall, not a flock_os code gap: one node event loop has to
// serialize 15k concurrent handshakes (TCP accept + engine.io OPEN + SIO
// CONNECT + per-room JOIN), so connect p95 balloons to ~27 s and <1 % of
// sessions ever establish. The auth cache (FLO-116) already cleared the
// *auth-callback* wall; this cleared the *connection-setup* wall by scaling the
// socketio tier horizontally — N node socketio processes behind a WS-aware
// load balancer — and wiring `@socket.io/redis-adapter` so the cluster behaves
// as one logical io instance.
//
// Why the adapter is needed across processes
// ------------------------------------------
// Frappe's realtime server already fans a `frappe.publish_realtime` out via a
// Redis "events" pub/sub channel: EVERY node process subscribes and emits to
// its OWN local room members, so the publish→room path already crosses
// processes without help. What does NOT cross processes is socket.io's OWN
// room machinery — `io.to(room).emit(...)` / `socket.broadcast.to(room)...`
// issued from inside a connection handler, plus room-membership coordination —
// which is per-process by default. `@socket.io/redis-adapter` routes those
// through Redis pub/sub so a broadcast originating on process A reaches the
// sockets that joined the room on process B. It is the standard socket.io
// multi-process story and the defense-in-depth that keeps the tier correct as
// it scales (not just fast).
//
// This module is the flock_os-owned wrapper around `@socket.io/redis-adapter`'s
// `createAdapter`. It keeps the wiring line in vendored Frappe
// (`scripts/dev/wire-socketio-redis-adapter.sh`) to a single guarded factory
// call, centralizes the opts (Redis key prefix / timeouts) so every backend in
// the cluster agrees, and turns a missing npm package into a loud, actionable
// error instead of a swallowed no-op. `@socket.io/redis-adapter` is required
// LAZILY so importing this module never throws when the package is absent
// (keeps `node --test` + CI package-free); the runtime wiring only fails when
// the adapter is actually attached.
//
// The pub/sub clients are INJECTED by the wiring (created from the bench's
// `redis_socketio` URL via frappe's `get_redis_subscriber`), so this module
// stays decoupled from frappe's path and unit-testable with fakes — the same
// injection style the auth cache (`realtime/middlewares/flock_auth_cache.js`)
// uses for `authenticate` and the room handler uses for `frappe_request`.
//
// Runbook: docs/development/ws-broadcast-delivery.md -> Scaling the socketio tier.
"use strict";

// The Redis pub/sub channel prefix socket.io-redis-adapter keys its
// coordination messages on. Every backend in the cluster MUST agree on it or
// their room/broadcast coordination messages never meet. The package default
// is `socket.io`; we allow an env override (e.g. to isolate two socket.io
// tenants sharing one Redis) but keep the default so a mixed-version cluster
// still coordinates. Read at attach time -> a config change is a process
// restart, not a per-connection branch.
const DEFAULT_KEY = "socket.io";
const DEFAULT_REQUESTS_TIMEOUT_MS = 5000;

function _envStr(name, fallback) {
	const raw = process.env[name];
	return raw || fallback;
}

function _envInt(name, fallback) {
	const raw = process.env[name];
	if (!raw) return fallback;
	const n = Number.parseInt(raw, 10);
	return Number.isFinite(n) && n > 0 ? n : fallback;
}

// Lazily resolve `@socket.io/redis-adapter` from the flock_os app's own
// node_modules (repo-root `node_modules`, reached via Node's walk-up
// resolution from realtime/adapters/). Kept lazy + separate so importing this
// module is side-effect-free under `node --test` where the package is absent,
// and so a genuinely missing dependency surfaces a clear error at attach time
// instead of a generic MODULE_NOT_FOUND deep in socket.io.
function requireRedisAdapter() {
	let pkg;
	try {
		pkg = require("@socket.io/redis-adapter");
	} catch (err) {
		const code = err && err.code;
		if (code === "MODULE_NOT_FOUND") {
			throw Object.assign(
				new Error(
					"flock_os realtime redis-adapter: '@socket.io/redis-adapter' is not installed " +
						"— run `npm install` in the flock_os app root (the repo root) so the scaled " +
						"socketio tier can fan broadcasts across node workers (FLO-121).",
				),
				{ code: "FLOCK_REDIS_ADAPTER_MISSING" },
			);
		}
		throw err;
	}
	if (typeof pkg.createAdapter !== "function") {
		throw new Error(
			"flock_os realtime redis-adapter: '@socket.io/redis-adapter' resolved but exports no " +
				"`createAdapter` — version mismatch? Expected @socket.io/redis-adapter v8 (FLO-121).",
		);
	}
	return pkg;
}

// Pure opts builder (unit-tested). Every backend in the cluster must build the
// SAME opts or their coordination pub/sub channels diverge — so the defaults
// are deterministic and the only overrides are env-sourced (cluster-wide).
function resolveAdapterOptions(opts = {}) {
	const key = opts.key ?? _envStr("FLOCK_SIO_ADAPTER_KEY", DEFAULT_KEY);
	const requestsTimeout =
		opts.requestsTimeout ?? _envInt("FLOCK_SIO_ADAPTER_REQUESTS_TIMEOUT_MS", DEFAULT_REQUESTS_TIMEOUT_MS);
	const resolved = { key, requestsTimeout };
	// `publishOnSpecificResponseChannel` keeps adapter behavior forward-compat;
	// leaving it to the package default unless explicitly set.
	if (opts.publishOnSpecificResponseChannel !== undefined) {
		resolved.publishOnSpecificResponseChannel = Boolean(opts.publishOnSpecificResponseChannel);
	}
	return resolved;
}

// Resolve the adapter's pub/sub Redis clients for the scaled socketio tier.
//
// The §8 15k burst stalls the shared dev Redis — this bench's `redis_socketio`
// == `redis_cache` (`127.0.0.1:13000`) — under 8x adapter pub/sub + Frappe
// cache, so a Redis client emits `ETIMEDOUT` (the runbook's local-testbed
// ceiling #2). The production tier gives the adapter a DEDICATED Redis so the
// socket.io coordination traffic never contends with the cache/queue instance
// (FLO-127 / FLO-10 §8 prod gate). This resolver picks the adapter's Redis:
//
//   1. `FLOCK_SIO_ADAPTER_REDIS` env  — a raw `redis://` URL for the dedicated
//      adapter instance (highest priority; matches the runbook name). Set it on
//      EVERY backend so the cluster agrees. `start-adapter-redis.sh` brings one
//      up on a new port and prints the URL to export.
//   2. `get_redis_subscriber(confKey)`  — the Frappe-idiomatic conf-keyed path:
//      add a `redis_socketio_adapter` key to `sites/common_site_config.json`
//      and point `FLOCK_SIO_ADAPTER_CONF_KEY` at it. Falls back to the bench's
//      `redis_socketio` (the current single-instance behavior) so nothing
//      changes unless a dedicated Redis is configured.
//
// `deps` is INJECTED (`get_redis_subscriber` + `createRedisClient`) so this
// stays unit-testable with fakes and has no frappe / `@redis/client` import at
// module load. The wiring (`apps/frappe/realtime/index.js`) supplies both
// because both are already in scope there (`get_redis_subscriber` is
// destructured from `../node_utils`; `@redis/client` is frappe's own dep, so
// `require("@redis/client").createClient` resolves from `apps/frappe/`).
// Returns `[pubClient, subClient]`, NOT yet connected — the wiring connects
// them (socket.io-redis-adapter tolerates a not-yet-connected client).
function resolveAdapterClients(deps = {}) {
	const dedicatedUrl = process.env.FLOCK_SIO_ADAPTER_REDIS;
	if (dedicatedUrl) {
		const createRedisClient = deps.createRedisClient;
		if (typeof createRedisClient !== "function") {
			throw Object.assign(
				new Error(
					"flock_os realtime redis-adapter: FLOCK_SIO_ADAPTER_REDIS is set to a dedicated URL but " +
						"no createRedisClient was injected — the wiring must pass " +
						"require('@redis/client').createClient so the URL can become clients (FLO-127).",
				),
				{ code: "FLOCK_REDIS_ADAPTER_NO_FACTORY" },
			);
		}
		const pub = createRedisClient({ url: dedicatedUrl });
		const sub = typeof pub.duplicate === "function" ? pub.duplicate() : createRedisClient({ url: dedicatedUrl });
		return [pub, sub];
	}
	const get_redis_subscriber = deps.get_redis_subscriber;
	if (typeof get_redis_subscriber !== "function") {
		throw Object.assign(
			new Error(
				"flock_os realtime redis-adapter: get_redis_subscriber is required when " +
					"FLOCK_SIO_ADAPTER_REDIS is unset — the wiring must pass frappe's get_redis_subscriber (FLO-127).",
			),
			{ code: "FLOCK_REDIS_ADAPTER_NO_FACTORY" },
		);
	}
	const confKey = _envStr("FLOCK_SIO_ADAPTER_CONF_KEY", "redis_socketio");
	const pub = get_redis_subscriber(confKey);
	const sub = typeof pub.duplicate === "function" ? pub.duplicate() : get_redis_subscriber(confKey);
	return [pub, sub];
}

// Build the socket.io-redis-adapter and return it for `io.adapter(...)`.
//
// `pubClient` / `subClient` are node-redis v4 clients (created + connected by
// the wiring from the bench's `redis_socketio` URL). They are injected so this
// module has no frappe/redis dependency at import time. `opts` overrides the
// cluster-wide defaults; `_deps.createAdapter` is the test seam (defaults to
// the lazily-required real package).
function createRedisAdapter(pubClient, subClient, opts = {}, _deps = {}) {
	if (!pubClient || !subClient) {
		throw new Error("flock_os realtime redis-adapter: pubClient and subClient are required");
	}
	const createAdapter = _deps.createAdapter ?? requireRedisAdapter().createAdapter;
	return createAdapter(pubClient, subClient, resolveAdapterOptions(opts));
}

module.exports = {
	createRedisAdapter,
	resolveAdapterClients,
	resolveAdapterOptions,
	requireRedisAdapter,
	DEFAULT_KEY,
	DEFAULT_REQUESTS_TIMEOUT_MS,
};
