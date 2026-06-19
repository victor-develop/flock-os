// Unit tests for the flock_os realtime Redis adapter wrapper (FLO-121).
//
// Runs under plain `node --test` — no bench, no socket.io, no Redis, no npm
// install — because the wrapper's logic is pure once `createAdapter` is
// injected. Mirrors the auth-cache + room-handler test approach
// (`realtime/middlewares/flock_auth_cache.test.mjs`).
//
//   node --test realtime/adapters/flock_redis_adapter.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);

import {
	createRedisAdapter,
	resolveAdapterClients,
	resolveAdapterOptions,
	requireRedisAdapter,
	DEFAULT_KEY,
	DEFAULT_REQUESTS_TIMEOUT_MS,
} from "./flock_redis_adapter.js";

// Records the (pub, sub, opts) the underlying createAdapter received, so a test
// can assert the wrapper forwarded the clients + built the right opts.
function recordingCreateAdapter(sink) {
	return function createAdapter(pub, sub, opts) {
		sink.push({ pub, sub, opts });
		return { __adapter: true, pub, sub, opts };
	};
}

// --- resolveAdapterOptions ------------------------------------------------- #
test("resolveAdapterOptions uses cluster-wide defaults", () => {
	const opts = resolveAdapterOptions();
	assert.equal(opts.key, DEFAULT_KEY);
	assert.equal(opts.requestsTimeout, DEFAULT_REQUESTS_TIMEOUT_MS);
});

test("resolveAdapterOptions honors explicit opts over env", () => {
	process.env.FLOCK_SIO_ADAPTER_KEY = "env-prefix";
	process.env.FLOCK_SIO_ADAPTER_REQUESTS_TIMEOUT_MS = "1234";
	try {
		// Explicit opts win over env (a caller override is intentional).
		const opts = resolveAdapterOptions({ key: "explicit", requestsTimeout: 42 });
		assert.equal(opts.key, "explicit");
		assert.equal(opts.requestsTimeout, 42);

		// Absent explicit opts -> env applies (cluster-wide config).
		const envOpts = resolveAdapterOptions();
		assert.equal(envOpts.key, "env-prefix");
		assert.equal(envOpts.requestsTimeout, 1234);
	} finally {
		delete process.env.FLOCK_SIO_ADAPTER_KEY;
		delete process.env.FLOCK_SIO_ADAPTER_REQUESTS_TIMEOUT_MS;
	}
});

test("resolveAdapterOptions ignores non-positive env ints", () => {
	process.env.FLOCK_SIO_ADAPTER_REQUESTS_TIMEOUT_MS = "garbage";
	try {
		assert.equal(resolveAdapterOptions().requestsTimeout, DEFAULT_REQUESTS_TIMEOUT_MS);
	} finally {
		delete process.env.FLOCK_SIO_ADAPTER_REQUESTS_TIMEOUT_MS;
	}
});

test("resolveAdapterOptions forwards publishOnSpecificResponseChannel only when set", () => {
	assert.equal(resolveAdapterOptions().publishOnSpecificResponseChannel, undefined);
	assert.equal(
		resolveAdapterOptions({ publishOnSpecificResponseChannel: true }).publishOnSpecificResponseChannel,
		true,
	);
});

// --- resolveAdapterClients ------------------------------------------------- #

// A fake frappe `get_redis_subscriber(kind)` that records the conf key and
// returns a client whose `duplicate()` yields a marked clone (node-redis v4
// shape). The wiring relies on `duplicate()` so the pub/sub pair shares config.
function fakeSubscriberFactory(calls) {
	return function get_redis_subscriber(kind) {
		calls.push(kind);
		return {
			kind,
			duplicate() {
				return { kind, duplicated: true };
			},
		};
	};
}

// A fake `@redis/client`.createClient({ url }) that records the URL and returns
// a client with a working `duplicate()` — mirrors the dedicated-Redis path.
function fakeRedisClientFactory(calls) {
	return function createRedisClient(opts) {
		calls.push(opts);
		const client = { url: opts && opts.url, duplicated: false };
		client.duplicate = function () {
			return { url: opts && opts.url, duplicated: true };
		};
		return client;
	};
}

test("resolveAdapterClients defaults to get_redis_subscriber(redis_socketio)", () => {
	const subCalls = [];
	const [pub, sub] = resolveAdapterClients({ get_redis_subscriber: fakeSubscriberFactory(subCalls) });
	assert.deepEqual(subCalls, ["redis_socketio"]);
	assert.equal(pub.kind, "redis_socketio");
	assert.equal(sub.duplicated, true);
	assert.equal(sub.kind, "redis_socketio");
});

test("resolveAdapterClients honors FLOCK_SIO_ADAPTER_CONF_KEY", () => {
	process.env.FLOCK_SIO_ADAPTER_CONF_KEY = "redis_socketio_adapter";
	try {
		const subCalls = [];
		const [pub, sub] = resolveAdapterClients({ get_redis_subscriber: fakeSubscriberFactory(subCalls) });
		assert.deepEqual(subCalls, ["redis_socketio_adapter"]);
		assert.equal(pub.kind, "redis_socketio_adapter");
		assert.equal(sub.kind, "redis_socketio_adapter");
	} finally {
		delete process.env.FLOCK_SIO_ADAPTER_CONF_KEY;
	}
});

test("resolveAdapterClients uses a dedicated Redis when FLOCK_SIO_ADAPTER_REDIS is set", () => {
	process.env.FLOCK_SIO_ADAPTER_REDIS = "redis://127.0.0.1:13010";
	try {
		const subCalls = [];
		const clientCalls = [];
		const factory = fakeSubscriberFactory(subCalls);
		// get_redis_subscriber must NOT be called when a dedicated URL is set.
		const [pub, sub] = resolveAdapterClients({
			get_redis_subscriber: factory,
			createRedisClient: fakeRedisClientFactory(clientCalls),
		});
		assert.deepEqual(subCalls, [], "dedicated path must bypass get_redis_subscriber");
		assert.deepEqual(clientCalls, [{ url: "redis://127.0.0.1:13010" }]);
		assert.equal(pub.url, "redis://127.0.0.1:13010");
		assert.equal(pub.duplicated, false);
		assert.equal(sub.url, "redis://127.0.0.1:13010");
		assert.equal(sub.duplicated, true);
	} finally {
		delete process.env.FLOCK_SIO_ADAPTER_REDIS;
	}
});

test("resolveAdapterClients: a client without duplicate() falls back to the factory", () => {
	// Defensive branch: if some client impl lacks duplicate(), build a second
	// client via the same factory instead of crashing.
	process.env.FLOCK_SIO_ADAPTER_REDIS = "redis://127.0.0.1:13010";
	try {
		const clientCalls = [];
		const createRedisClient = function (opts) {
			clientCalls.push(opts);
			return { url: opts && opts.url }; // NOTE: no duplicate()
		};
		const [pub, sub] = resolveAdapterClients({ createRedisClient });
		assert.equal(clientCalls.length, 2);
		assert.equal(pub.url, "redis://127.0.0.1:13010");
		assert.equal(sub.url, "redis://127.0.0.1:13010");
	} finally {
		delete process.env.FLOCK_SIO_ADAPTER_REDIS;
	}
});

test("resolveAdapterClients throws a loud, actionable error when no factory is injected", () => {
	// Dedicated URL set but createRedisClient missing.
	process.env.FLOCK_SIO_ADAPTER_REDIS = "redis://127.0.0.1:13010";
	try {
		assert.throws(
			() => resolveAdapterClients({ get_redis_subscriber: fakeSubscriberFactory([]) }),
			(err) => err.code === "FLOCK_REDIS_ADAPTER_NO_FACTORY" && /createRedisClient/.test(err.message),
		);
	} finally {
		delete process.env.FLOCK_SIO_ADAPTER_REDIS;
	}

	// No dedicated URL and no get_redis_subscriber.
	assert.throws(
		() => resolveAdapterClients({}),
		(err) => err.code === "FLOCK_REDIS_ADAPTER_NO_FACTORY" && /get_redis_subscriber/.test(err.message),
	);
});

// --- createRedisAdapter ---------------------------------------------------- #
test("createRedisAdapter forwards pub/sub clients + built opts to the adapter", () => {
	const sink = [];
	const pub = { __pub: true };
	const sub = { __sub: true };
	const adapter = createRedisAdapter(pub, sub, { key: "flock_os", requestsTimeout: 99 }, {
		createAdapter: recordingCreateAdapter(sink),
	});

	assert.equal(adapter.__adapter, true);
	assert.equal(sink.length, 1);
	assert.equal(sink[0].pub, pub);
	assert.equal(sink[0].sub, sub);
	assert.equal(sink[0].opts.key, "flock_os");
	assert.equal(sink[0].opts.requestsTimeout, 99);
});

test("createRedisAdapter rejects missing clients", () => {
	assert.throws(
		() => createRedisAdapter(null, {}, {}, { createAdapter: recordingCreateAdapter([]) }),
		/pubClient and subClient are required/,
	);
	assert.throws(
		() => createRedisAdapter({}, undefined, {}, { createAdapter: recordingCreateAdapter([]) }),
		/pubClient and subClient are required/,
	);
});

// --- requireRedisAdapter (missing-package -> loud, actionable error) ------- #
test("requireRedisAdapter surfaces a clear error when the package is missing", () => {
	// Shadow Node's resolver so the MODULE_NOT_FOUND branch fires deterministically,
	// regardless of whether the package is installed in this checkout.
	const Module = require("node:module");
	const realResolve = Module._resolveFilename;
	Module._resolveFilename = function (req, ...rest) {
		if (req === "@socket.io/redis-adapter") {
			const err = new Error("Cannot find module");
			err.code = "MODULE_NOT_FOUND";
			throw err;
		}
		return realResolve(req, ...rest);
	};
	try {
		assert.throws(() => requireRedisAdapter(), (err) => {
			assert.equal(err.code, "FLOCK_REDIS_ADAPTER_MISSING");
			assert.match(err.message, /@socket.io\/redis-adapter/);
			assert.match(err.message, /npm install/);
			return true;
		});
	} finally {
		Module._resolveFilename = realResolve;
	}
});

test("requireRedisAdapter fails loud on a non-function createAdapter export", () => {
	// A fake module that exports no `createAdapter` -> version-mismatch branch.
	const os = require("node:os");
	const fs = require("node:fs");
	const path = require("node:path");
	const fake = path.join(os.tmpdir(), `flock-fake-adapter-${process.pid}-${Date.now()}.js`);
	fs.writeFileSync(fake, "module.exports = {};", "utf8");

	const Module = require("node:module");
	const realResolve = Module._resolveFilename;
	Module._resolveFilename = function (req, ...rest) {
		if (req === "@socket.io/redis-adapter") return fake;
		return realResolve(req, ...rest);
	};
	try {
		assert.throws(() => requireRedisAdapter(), /exports no `createAdapter`/);
	} finally {
		Module._resolveFilename = realResolve;
		fs.unlinkSync(fake);
	}
});
