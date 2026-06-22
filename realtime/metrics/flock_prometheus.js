// flock_os per-worker Prometheus metrics surface for the node socketio tier
// (FLO-922 Phase 6.2 — design §6 gap G1).
//
// Why this exists: FLO-897 wired the APP-side metrics (gunicorn's view of the
// world: per-route-class HTTP counters, RQ depth, MariaDB, cache hit ratio).
// What it CANNOT see is the per-worker socket.io engine internals —
// `io.engine.clientsCount`, per-room counts, connect/disconnect rate, and
// broadcast fan-out. Those live INSIDE each node socketio worker. The design
// (metrics-alerting-design.md §1.4 + §6 G1) names this as the highest-priority
// instrumentation gap because it is the scrape source for the four critical
// §8 WS-SLO alerts (WSConnectSLOBreach, WSBroadcastSLOBreach,
// WSErrorCounterNonZero, WSessionsDropped). Without it those alerts have no
// metric to arm against.
//
// The flock_os realtime tier runs N node socketio workers behind the nginx
// WS-upgrade LB (FLO-121). Each worker is a separate node process running
// apps/frappe/socketio.js, so this module is ATTACHED once per process via a
// guarded require wired into apps/frappe/realtime/index.js
// (scripts/dev/wire-socketio-metrics.sh — same marker-guarded shape as the
// auth-cache / room-handler / redis-adapter wirings). It:
//
//   1. Lazily requires `prom-client` and builds a PRIVATE Registry (so it does
//      not collide with any default registry or another app on the same
//      process).
//   2. Defines the per-worker metric set (below) — the §1.4 gauges/counters
//      that gate the WS-SLO alerts.
//   3. Hooks engine.io events (connection / close / connection_error) and a
//      periodic rooms refresh.
//   4. Starts a SEPARATE http.Server on FLOCK_SIO_METRICS_PORT (default 9100)
//      serving `GET /metrics` as Prometheus text exposition — Prometheus
//      scrapes one URL per worker (docker DNS `socketio-N:9100`).
//
// This module is import-clean (lazy prom-client require + side-effect-free
// builder) so it runs under `node --test` with prom-client absent; the runtime
// attach only fails when the metrics endpoint is actually stood up.
//
// Runbook: docs/operations/production-instrumentation.md (FLO-922).
"use strict";

const DEFAULT_PORT = 9100;
const DEFAULT_REFRESH_MS = 5000;

function _envInt(name, fallback) {
	const raw = process.env[name];
	if (!raw) return fallback;
	const n = Number.parseInt(raw, 10);
	return Number.isFinite(n) && n > 0 ? n : fallback;
}

// Lazily resolve `prom-client` from the flock_os app's own node_modules
// (repo-root `node_modules`, reached via Node's walk-up resolution from
// realtime/metrics/). Kept lazy + isolated so importing this module is
// side-effect-free under `node --test` where prom-client is absent, and so a
// genuinely missing dependency surfaces a clear error at attach time instead
// of a generic MODULE_NOT_FOUND deep in prom-client's own require chain.
function requirePromClient() {
	let pkg;
	try {
		pkg = require("prom-client");
	} catch (err) {
		const code = err && err.code;
		if (code === "MODULE_NOT_FOUND") {
			throw Object.assign(
				new Error(
					"flock_os realtime metrics: 'prom-client' is not installed — run `npm install` " +
						"in the flock_os app root (the repo root) so the scaled socketio tier can " +
						"expose its per-worker /metrics endpoint (FLO-922 / FLO-586 §6 G1).",
				),
				{ code: "FLOCK_PROM_CLIENT_MISSING" },
			);
		}
		throw err;
	}
	// v14+ exposes `Registry` (class) + `register` (default Registry instance).
	// Older or incompatible builds lack the Gauge/Counter factories we need.
	if (
		typeof pkg.Registry !== "function" ||
		typeof pkg.Gauge !== "function" ||
		typeof pkg.Counter !== "function"
	) {
		throw new Error(
			"flock_os realtime metrics: 'prom-client' resolved but has an unexpected shape — " +
				"version mismatch? Expected prom-client v14+ (Registry + Gauge + Counter).",
		);
	}
	return pkg;
}

// Build the per-worker metric set against a prom-client Registry. Pure: takes
// the package + registry, returns the metric handles + a refresh(io) function
// the caller drives on a timer / on engine events.
//
// `pkg` is injected so the unit suite can pass a fake prom-client. The metric
// NAMES are pinned to the FLO-586 / FLO-897 taxonomy so dashboards + alert
// rules written against that doc resolve against this scrape unchanged.
function buildMetricSet(pkg, registry) {
	const connectionsActive = new pkg.Gauge({
		name: "flock_ws_connections_active",
		help: "Active socket.io connections on this worker (io.engine.clientsCount).",
		registers: [registry],
	});
	const connectsTotal = new pkg.Counter({
		name: "flock_ws_connects_total",
		help: "Cumulative engine.io connection-establishment events on this worker.",
		registers: [registry],
	});
	const disconnectsTotal = new pkg.Counter({
		name: "flock_ws_disconnects_total",
		help: "Cumulative engine.io connection-close events on this worker.",
		registers: [registry],
	});
	const rooms = new pkg.Gauge({
		name: "flock_ws_rooms",
		help: "Active socket.io rooms tracked by this worker's adapter.",
		registers: [registry],
	});
	const broadcastFanoutTotal = new pkg.Counter({
		name: "flock_ws_broadcast_fanout_total",
		help: "Cumulative server-side broadcast fan-out events on this worker, by channel class.",
		labelNames: ["channel"],
		registers: [registry],
	});
	const receiveErrorsTotal = new pkg.Counter({
		name: "flock_ws_receive_errors_total",
		help: "Cumulative socket.io receive-error events observed on this worker (launch-gate signal S6).",
		registers: [registry],
	});

	// Refresh the gauges whose source is io state. Called on a timer + on
	// engine events. Counters auto-increment via inc(); gauges must be polled
	// because their backing value lives inside io's adapter and changes without
	// firing an event we can hook (e.g. room add via socket.join).
	function refresh(io) {
		if (!io) return;
		try {
			const clientsCount = io.engine && typeof io.engine.clientsCount === "number"
				? io.engine.clientsCount
				: 0;
			connectionsActive.set(clientsCount);
		} catch {
			/* best-effort: never let a metrics refresh down the worker */
		}
		try {
			// socket.io v4: the per-namespace SioAdapter lives at
			// io.sockets.adapter; its rooms Map holds the active room set.
			const adapter = io.sockets && io.sockets.adapter;
			let count = 0;
			if (adapter && typeof adapter.size === "number") {
				count = adapter.size;
			} else if (adapter && adapter.rooms && typeof adapter.rooms.size === "number") {
				count = adapter.rooms.size;
			}
			rooms.set(count);
		} catch {
			/* best-effort */
		}
	}

	return {
		connectionsActive,
		connectsTotal,
		disconnectsTotal,
		rooms,
		broadcastFanoutTotal,
		receiveErrorsTotal,
		refresh,
	};
}

// Track the running metrics attach per-process so attachMetrics is idempotent
// (a hot-reload or a double wire does not double-bind the port). Module-scope
// matches the "one io per process" assumption of the Frappe realtime server.
let _attached = null;

// Attach the metrics surface to a socket.io `io` instance.
//
// `io` is the socket.io Server (or the parent http server) whose engine we
// observe. `opts.port` overrides FLOCK_SIO_METRICS_PORT. `deps` is the test
// seam ({ pkg, createServer, setInterval }). Returns the attach record
// ({ registry, metrics, server, port, timer }) so the caller can drive /
// assert against it.
//
// Failures are NON-FATAL to the realtime path: a missing prom-client or a
// /metrics port bind error logs + returns null. The realtime tier must keep
// serving traffic even if the metrics endpoint is down — observability is a
// support function, not a critical path.
function attachMetrics(io, opts = {}, deps = {}) {
	if (_attached) return _attached;
	let pkg;
	try {
		pkg = deps.pkg || requirePromClient();
	} catch (err) {
		console.error("flock_os realtime metrics:", (err && err.message) || err);
		return null;
	}
	const registry = new pkg.Registry();
	const metrics = buildMetricSet(pkg, registry);

	// Hook engine.io events. socket.io v4 surfaces them on io.engine.
	const engine = io && io.engine;
	if (engine && typeof engine.on === "function") {
		engine.on("connection", () => {
			metrics.connectsTotal.inc();
			metrics.refresh(io);
		});
		// socket.io v4: engine fires `connection_error` for every failed
		// handshake (bad session, auth reject, transport protocol error).
		// Each one is a §8 S6 receive-error datapoint.
		engine.on("connection_error", () => {
			metrics.receiveErrorsTotal.inc();
		});
	}
	// Per-socket close: socket.io v4 fires the engine-level `close` per
	// transport. Hook the namespace's connection event for a robust per-socket
	// disconnect counter (the engine close fires on transport teardown, which
	// can lag the namespace-level disconnect).
	try {
		const ns = io && typeof io.of === "function" ? io.of("/") : null;
		if (ns && typeof ns.on === "function") {
			ns.on("connection", (socket) => {
				if (socket && typeof socket.on === "function") {
					socket.on("disconnect", () => {
						metrics.disconnectsTotal.inc();
						metrics.refresh(io);
					});
				}
			});
		}
	} catch {
		/* best-effort */
	}

	const setIntervalFn = deps.setInterval || setInterval;
	const intervalMs = _envInt("FLOCK_SIO_METRICS_REFRESH_MS", DEFAULT_REFRESH_MS);
	const timer = setIntervalFn(() => metrics.refresh(io), intervalMs);
	if (typeof timer.unref === "function") timer.unref();

	// Stand up /metrics on a dedicated port — distinct from the socket.io port
	// so it can be scraped without crossing the WS upgrade path, and so
	// Prometheus reaches each worker directly (no LB).
	const http = require("node:http");
	const createServer = deps.createServer || http.createServer;
	const port = _envInt("FLOCK_SIO_METRICS_PORT", opts.port || DEFAULT_PORT);
	const server = createServer((req, res) => {
		if (req.method !== "GET" || req.url !== "/metrics") {
			res.statusCode = 404;
			res.setHeader("Content-Type", "text/plain");
			res.end("not found\n");
			return;
		}
		metrics.refresh(io);
		const body = typeof registry.metrics === "function" ? registry.metrics() : "";
		const ct = typeof registry.contentType === "string" ? registry.contentType : "text/plain; version=0.0.4";
		// prom-client v14 returns a Promise from metrics(); accept either.
		Promise.resolve(body).then(
			(text) => {
				res.statusCode = 200;
				res.setHeader("Content-Type", ct);
				res.end(text);
			},
			(err) => {
				res.statusCode = 500;
				res.setHeader("Content-Type", "text/plain");
				res.end(`metrics error: ${(err && err.message) || err}\n`);
			},
		);
	});
	server.on("error", (err) => {
		console.error("flock_os realtime metrics /metrics server:", (err && err.message) || err);
	});
	try {
		server.listen(port);
	} catch (err) {
		console.error("flock_os realtime metrics: could not listen on :" + port + ":", (err && err.message) || err);
	}
	_attached = { registry, metrics, server, port, timer, clearIntervalFn: deps.clearInterval || clearInterval };
	console.log(`flock_os realtime metrics: /metrics listening on :${port}`);
	return _attached;
}

function detachMetrics() {
	if (!_attached) return;
	const { server, timer, clearIntervalFn } = _attached;
	try { if (typeof clearIntervalFn === "function") clearIntervalFn(timer); } catch { /* best-effort */ }
	try { server.close(); } catch { /* best-effort */ }
	_attached = null;
}

// Test seam: reset the module-level attach state. Only used by the unit suite.
function _resetAttachedForTest() {
	_attached = null;
}

module.exports = {
	attachMetrics,
	detachMetrics,
	buildMetricSet,
	requirePromClient,
	DEFAULT_PORT,
	DEFAULT_REFRESH_MS,
	_resetAttachedForTest,
};
