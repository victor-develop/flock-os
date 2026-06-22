// Unit tests for the flock_os realtime per-worker /metrics surface (FLO-922).
//
// Runs under plain `node --test` — no bench, no socket.io, no Redis, no npm
// install — because the metrics surface's logic is pure once `prom-client` +
// `createServer` are injected. Mirrors the adapter + auth-cache test approach
// (`realtime/adapters/flock_redis_adapter.test.mjs`).
//
//   node --test realtime/metrics/flock_prometheus.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);

import {
	attachMetrics,
	detachMetrics,
	buildMetricSet,
	requirePromClient,
	DEFAULT_PORT,
	_resetAttachedForTest,
} from "./flock_prometheus.js";

// ---- minimal prom-client fake --------------------------------------------- #
// The real prom-client is heavy + requires npm install; for unit tests a
// minimal stand-in that records every metric registered against it suffices.
// The shape we exercise: Registry, Gauge, Counter; registry.metrics() returns
// a deterministic string; registry.contentType is a string.
function makeFakePromClient() {
	class FakeRegistry {
		constructor() {
			this.metrics = () => "# flock fake registry\n";
			this.contentType = "text/plain; version=0.0.4; flock-test";
		}
	}
	let nextId = 0;
	class FakeMetric {
		constructor({ name, help, labelNames = [] }) {
			this.name = name;
			this.help = help;
			this.labelNames = labelNames;
			this.value = 0;
			this.incCalls = 0;
			this.setCalls = 0;
			this.id = ++nextId;
		}
		inc(by = 1) { this.incCalls += 1; this.value += by; }
		set(v) { this.setCalls += 1; this.value = v; }
		labels() { return this; }
	}
	return {
		Registry: FakeRegistry,
		Gauge: class FakeGauge extends FakeMetric {},
		Counter: class FakeCounter extends FakeMetric {},
	};
}

// A minimal createServer that records the handler + lets a test invoke it
// synchronously. Returns an object with a `fire(req)` helper + `listen()` stub.
function makeFakeServerFactory(sink) {
	return function createServer(handler) {
		const errorListeners = [];
		const server = {
			handler,
			port: null,
			listen(port) { server.port = port; sink.port = port; },
			close() { server.port = null; },
			on(event, cb) {
				if (event === "error") errorListeners.push(cb);
				return server;
			},
			emitError(err) { for (const cb of errorListeners) cb(err); },
		};
		sink.servers.push(server);
		return server;
	};
}

// A minimal fake `io` with the engine/sockets surfaces prom-client scrapes.
function makeFakeIo({ clientsCount = 0, roomsSize = 0 } = {}) {
	const engineListeners = new Map();
	const nsListeners = new Map();
	return {
		engine: {
			clientsCount,
			on(event, cb) {
				engineListeners.set(event, cb);
			},
		},
		_socketsAdapterSize: roomsSize,
		_socketsAdapterRoomsSize: roomsSize,
		get sockets() {
			return {
				adapter: {
					size: this._socketsAdapterSize,
					rooms: { size: this._socketsAdapterRoomsSize },
				},
				on: (event, cb) => nsListeners.set(event, cb),
			};
		},
		_emitEngine(event, ...args) {
			const cb = engineListeners.get(event);
			if (cb) cb(...args);
		},
		_emitNs(event, ...args) {
			const cb = nsListeners.get(event);
			if (cb) cb(...args);
		},
		_of() { return null; },
		of() { return null; },
	};
}

// ---- buildMetricSet ------------------------------------------------------- #
test("buildMetricSet registers the six §1.4 metrics against the registry", () => {
	const pkg = makeFakePromClient();
	const registry = new pkg.Registry();
	const m = buildMetricSet(pkg, registry);

	// The names match the FLO-586 taxonomy + FLO-897 alerting.md; pinning
	// them here catches a rename that would silently break the dashboards /
	// alert rules wired in FLO-922.
	assert.equal(m.connectionsActive.name, "flock_ws_connections_active");
	assert.equal(m.connectsTotal.name, "flock_ws_connects_total");
	assert.equal(m.disconnectsTotal.name, "flock_ws_disconnects_total");
	assert.equal(m.rooms.name, "flock_ws_rooms");
	assert.equal(m.broadcastFanoutTotal.name, "flock_ws_broadcast_fanout_total");
	assert.equal(m.receiveErrorsTotal.name, "flock_ws_receive_errors_total");
	assert.deepEqual(m.broadcastFanoutTotal.labelNames, ["channel"]);
});

test("buildMetricSet.refresh(io) reads clientsCount + rooms off io", () => {
	const pkg = makeFakePromClient();
	const registry = new pkg.Registry();
	const m = buildMetricSet(pkg, registry);
	const io = makeFakeIo({ clientsCount: 42, roomsSize: 7 });

	m.refresh(io);

	assert.equal(m.connectionsActive.value, 42);
	assert.equal(m.connectionsActive.setCalls, 1);
	assert.equal(m.rooms.value, 7);
	assert.equal(m.rooms.setCalls, 1);
});

test("buildMetricSet.refresh() is best-effort on a malformed io (no throw)", () => {
	const pkg = makeFakePromClient();
	const registry = new pkg.Registry();
	const m = buildMetricSet(pkg, registry);

	// Each branch catches internally; a refresh against an io missing
	// engine/sockets must NOT throw (the realtime path never blocks on
	// metrics). The gauge is set to 0 when the source is missing — that is
	// the desired Prometheus behavior (a missing datapoint would silently
	// look like a stale series).
	assert.doesNotThrow(() => m.refresh(undefined));
	assert.doesNotThrow(() => m.refresh({}));
	assert.doesNotThrow(() => m.refresh({ engine: {} }));
	assert.equal(m.connectionsActive.value, 0, "missing source → 0 (not undefined)");
});

// ---- attachMetrics -------------------------------------------------------- #
test("attachMetrics wires engine events + /metrics server on the configured port", async () => {
	_resetAttachedForTest();
	const pkg = makeFakePromClient();
	const sink = { servers: [], port: null };
	const createServer = makeFakeServerFactory(sink);
	const io = makeFakeIo({ clientsCount: 3, roomsSize: 1 });

	const attached = attachMetrics(io, { port: 19123 }, { pkg, createServer, setInterval: () => 99 });

	assert.ok(attached, "returns an attach record");
	assert.equal(attached.port, 19123);
	assert.equal(sink.port, 19123, "http server.listen called with the port");

	// engine.connection fires -> connectsTotal.inc + refresh sets the gauge.
	io._emitEngine("connection");
	assert.equal(attached.metrics.connectsTotal.incCalls, 1);
	assert.equal(attached.metrics.connectionsActive.value, 3);

	// engine.connection_error -> receiveErrorsTotal.inc (signal S6).
	io._emitEngine("connection_error");
	assert.equal(attached.metrics.receiveErrorsTotal.incCalls, 1);

	// The /metrics handler exists and returns 200 with the registry content type.
	// prom-client v14 metrics() returns a Promise; the handler resolves it
	// asynchronously, so we await the body via a tiny promise.
	const server = sink.servers[0];
	const { res } = await new Promise((resolve) => {
		const res = { statusCode: null, headers: {}, body: "", setHeader(k, v) { this.headers[k] = v; }, end(b) { this.body = b; resolve({ res }); } };
		server.handler({ method: "GET", url: "/metrics" }, res);
	});
	assert.equal(res.statusCode, 200);
	assert.equal(res.headers["Content-Type"], "text/plain; version=0.0.4; flock-test");
	assert.match(res.body, /flock fake registry/);
});

test("attachMetrics: /metrics handler 404s non-/metrics paths (not an open proxy)", () => {
	_resetAttachedForTest();
	const pkg = makeFakePromClient();
	const sink = { servers: [], port: null };
	const io = makeFakeIo({});
	const attached = attachMetrics(io, {}, { pkg, createServer: makeFakeServerFactory(sink), setInterval: () => 1 });
	const server = sink.servers[0];
	const res = { statusCode: null, headers: {}, setHeader() {}, end(b) { this.body = b; } };
	server.handler({ method: "GET", url: "/" }, res);
	assert.equal(res.statusCode, 404);
	server.handler({ method: "POST", url: "/metrics" }, res);
	assert.equal(res.statusCode, 404);
});

test("attachMetrics honors FLOCK_SIO_METRICS_PORT over opts.port", () => {
	_resetAttachedForTest();
	const oldPort = process.env.FLOCK_SIO_METRICS_PORT;
	process.env.FLOCK_SIO_METRICS_PORT = "19200";
	try {
		const pkg = makeFakePromClient();
		const sink = { servers: [], port: null };
		const attached = attachMetrics(makeFakeIo(), { port: 19123 }, {
			pkg,
			createServer: makeFakeServerFactory(sink),
			setInterval: () => 1,
		});
		assert.equal(attached.port, 19200, "env port wins over opts.port");
		assert.equal(sink.port, 19200);
	} finally {
		if (oldPort === undefined) delete process.env.FLOCK_SIO_METRICS_PORT;
		else process.env.FLOCK_SIO_METRICS_PORT = oldPort;
	}
});

test("attachMetrics is idempotent (a second call returns the existing attach)", () => {
	_resetAttachedForTest();
	const pkg = makeFakePromClient();
	const sink = { servers: [], port: null };
	const createServer = makeFakeServerFactory(sink);
	attachMetrics(makeFakeIo(), {}, { pkg, createServer, setInterval: () => 1 });
	const second = attachMetrics(makeFakeIo(), {}, { pkg, createServer, setInterval: () => 1 });
	assert.equal(sink.servers.length, 1, "no second server bound");
	assert.ok(second);
});

test("attachMetrics: a missing prom-client is non-fatal (logs + returns null)", () => {
	_resetAttachedForTest();
	// Shadow Node's resolver so the MODULE_NOT_FOUND branch fires regardless of
	// whether prom-client is installed in this checkout.
	const Module = require("node:module");
	const realResolve = Module._resolveFilename;
	Module._resolveFilename = function (req, ...rest) {
		if (req === "prom-client") {
			const err = new Error("Cannot find module");
			err.code = "MODULE_NOT_FOUND";
			throw err;
		}
		return realResolve(req, ...rest);
	};
	const errors = [];
	const origErr = console.error;
	console.error = (...args) => errors.push(args);
	try {
		const attached = attachMetrics(makeFakeIo(), {}, {
			createServer: makeFakeServerFactory({ servers: [], port: null }),
			setInterval: () => 1,
		});
		assert.equal(attached, null, "non-fatal: returns null so the realtime tier keeps serving");
		assert.ok(errors.length >= 1, "logs the actionable missing-package error");
		assert.match(String(errors[0]), /prom-client/);
	} finally {
		Module._resolveFilename = realResolve;
		console.error = origErr;
	}
});

test("detachMetrics closes the server + clears the timer", () => {
	_resetAttachedForTest();
	const pkg = makeFakePromClient();
	const sink = { servers: [], port: null };
	let cleared = 0;
	const attached = attachMetrics(makeFakeIo(), { port: 19125 }, {
		pkg,
		createServer: makeFakeServerFactory(sink),
		setInterval: () => 99,
		clearInterval: () => { cleared += 1; },
	});
	const server = sink.servers[0];
	detachMetrics();
	assert.equal(server.port, null, "server closed");
	assert.equal(cleared, 1, "timer cleared");
	// Re-attach is now possible (idempotency state cleared).
	assert.equal(attached.server, server);
});

// ---- requirePromClient ---------------------------------------------------- #
test("requirePromClient surfaces a clear error when the package is missing", () => {
	const Module = require("node:module");
	const realResolve = Module._resolveFilename;
	Module._resolveFilename = function (req, ...rest) {
		if (req === "prom-client") {
			const err = new Error("Cannot find module");
			err.code = "MODULE_NOT_FOUND";
			throw err;
		}
		return realResolve(req, ...rest);
	};
	try {
		assert.throws(() => requirePromClient(), (err) => {
			assert.equal(err.code, "FLOCK_PROM_CLIENT_MISSING");
			assert.match(err.message, /prom-client/);
			assert.match(err.message, /npm install/);
			return true;
		});
	} finally {
		Module._resolveFilename = realResolve;
	}
});

test("requirePromClient rejects a prom-client without Gauge/Counter/Registry", () => {
	const os = require("node:os");
	const fs = require("node:fs");
	const path = require("node:path");
	const fake = path.join(os.tmpdir(), `flock-fake-prom-${process.pid}-${Date.now()}.js`);
	// Exports only `register` (a default Registry) but no Gauge/Counter/Registry
	// class — an older prom-client or an unrelated package with the same name.
	fs.writeFileSync(fake, "module.exports = { register: {} };", "utf8");
	const Module = require("node:module");
	const realResolve = Module._resolveFilename;
	Module._resolveFilename = function (req, ...rest) {
		if (req === "prom-client") return fake;
		return realResolve(req, ...rest);
	};
	try {
		assert.throws(() => requirePromClient(), /unexpected shape/);
	} finally {
		Module._resolveFilename = realResolve;
		fs.unlinkSync(fake);
	}
});

test("DEFAULT_PORT is the documented 9100", () => {
	assert.equal(DEFAULT_PORT, 9100);
});
