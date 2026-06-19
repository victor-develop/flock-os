// Unit tests for the flock_os realtime per-connection auth cache (FLO-116).
//
// Runs under plain `node --test` — no bench, no socket.io, no Frappe — because
// the cache + middleware branching is pure once frappe's `authenticate` is
// injected. Mirrors the room-join handler's test approach
// (`realtime/handlers/flock_room_handlers.test.mjs`).
//
//   node --test realtime/middlewares/flock_auth_cache.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";

import {
	wrap,
	AuthCache,
	cacheKey,
	DEFAULT_TTL_MS,
	DEFAULT_MAX,
} from "./flock_auth_cache.js";

// A fake socket carrying only what cacheKey / the middleware touch: the cookie
// + Authorization handshake headers, plus the identity fields frappe's
// authenticate populates on success.
function fakeSocket({ cookie, authorization } = {}) {
	const headers = {};
	if (cookie !== undefined) headers.cookie = cookie;
	if (authorization !== undefined) headers.authorization = authorization;
	return { request: { headers } };
}

// A fake frappe `authenticate(socket, next)` that, on success, populates the
// socket identity exactly like vendored authenticate.js does, then calls next().
// `calls` records every invocation so tests assert the HTTP path was skipped.
function fakeFrappeAuth(identity, calls = []) {
	return function authenticate(socket, next) {
		calls.push(socket);
		// Emulate the async resolution frappe performs via superagent.
		setImmediate(() => {
			socket.user = identity.user;
			socket.user_type = identity.user_type;
			socket.sid = identity.sid;
			socket.authorization_header = identity.authorization_header;
			next();
		});
	};
}

function failingFrappeAuth(err, calls = []) {
	return function authenticate(_socket, next) {
		calls.push(_socket);
		setImmediate(() => next(err));
	};
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// --- cacheKey --------------------------------------------------------------- #
test("cacheKey reads the sid cookie", () => {
	assert.equal(cacheKey(fakeSocket({ cookie: "sid=abc123" })), "abc123");
	assert.equal(
		cacheKey(fakeSocket({ cookie: "theme=dark; sid=abc123; other=1" })),
		"abc123",
	);
});

test("cacheKey falls back to the Authorization header when no sid cookie", () => {
	assert.equal(
		cacheKey(fakeSocket({ authorization: "token bearer-xyz" })),
		"token bearer-xyz",
	);
});

test("cacheKey returns null when nothing to key on", () => {
	assert.equal(cacheKey(fakeSocket()), null);
	assert.equal(cacheKey(fakeSocket({ cookie: "theme=dark" })), null);
});

// --- wrap: miss delegates + caches, hit replays without HTTP ---------------- #
test("first connection is a MISS: frappe authenticate runs and the result is cached", async () => {
	const calls = [];
	const identity = {
		user: "leader@flock.os",
		user_type: "System User",
		sid: "SID-1",
		authorization_header: undefined,
	};
	const middleware = wrap(fakeFrappeAuth(identity, calls), { ttlMs: 60_000 });

	const socket = fakeSocket({ cookie: "sid=SID-1" });
	await new Promise((r) => middleware(socket, r));

	assert.equal(calls.length, 1, "frappe authenticate runs on the miss");
	assert.equal(socket.user, "leader@flock.os");
	assert.equal(socket.user_type, "System User");
	assert.equal(socket.sid, "SID-1");
	assert.equal(middleware.cache.get("SID-1").user, "leader@flock.os");
	assert.equal(middleware.cache.stats.misses, 1);
	assert.equal(middleware.cache.stats.stores, 1);
});

test("second connection with the same sid is a HIT: frappe authenticate is NOT called", async () => {
	const calls = [];
	const identity = {
		user: "leader@flock.os",
		user_type: "System User",
		sid: "SID-1",
		authorization_header: undefined,
	};
	const middleware = wrap(fakeFrappeAuth(identity, calls), { ttlMs: 60_000 });

	const first = fakeSocket({ cookie: "sid=SID-1" });
	const second = fakeSocket({ cookie: "sid=SID-1" });
	await new Promise((r) => middleware(first, r));
	await new Promise((r) => middleware(second, r));

	assert.equal(calls.length, 1, "get_user_info fires once per session, not per connection");
	// The hit must still replay the full identity the room-join + frappe_handlers
	// paths depend on (socket.user drives `user:<u>` room auto-join).
	assert.equal(second.user, "leader@flock.os");
	assert.equal(second.user_type, "System User");
	assert.equal(second.sid, "SID-1");
	assert.equal(middleware.cache.stats.hits, 1);
});

test("a 15k-style burst of same-sid connections makes exactly ONE frappe authenticate call", async () => {
	const calls = [];
	const identity = { user: "leader@flock.os", user_type: "System User", sid: "S", authorization_header: undefined };
	const middleware = wrap(fakeFrappeAuth(identity, calls));

	const N = 1000; // stands in for 15k; the branching is identical
	for (let i = 0; i < N; i++) {
		await new Promise((r) => middleware(fakeSocket({ cookie: "sid=S" }), r));
	}
	assert.equal(calls.length, 1, "1 call + (N-1) cache hits — the §8 wall cleared");
	assert.equal(middleware.cache.stats.hits, N - 1);
});

// --- wrap: distinct sessions + error path ----------------------------------- #
test("distinct sids cache independently and never cross-replay", async () => {
	const calls = [];
	const resolve = (sid, user) => fakeFrappeAuth({ user, user_type: "System User", sid, authorization_header: undefined }, calls);
	// Alternate the resolved identity by sid via a custom auth.
	const auth = (socket, next) => {
		calls.push(socket);
		const sid = /sid=([^;]+)/.exec(socket.request.headers.cookie || "")[1];
		const id = { user: `u-${sid}`, user_type: "System User", sid, authorization_header: undefined };
		setImmediate(() => {
			Object.assign(socket, id);
			next();
		});
	};
	const middleware = wrap(auth);

	await new Promise((r) => middleware(fakeSocket({ cookie: "sid=A" }), r));
	await new Promise((r) => middleware(fakeSocket({ cookie: "sid=B" }), r));
	await new Promise((r) => middleware(fakeSocket({ cookie: "sid=A" }), r));
	await new Promise((r) => middleware(fakeSocket({ cookie: "sid=B" }), r));

	assert.equal(calls.length, 2, "one call per distinct session");
	assert.equal(middleware.cache.size, 2);
});

test("on frappe authenticate error, nothing is cached and next receives the error", async () => {
	const calls = [];
	const middleware = wrap(failingFrappeAuth(new Error("Unauthorized: 401"), calls));
	const socket = fakeSocket({ cookie: "sid=BAD" });

	const err = await new Promise((r) => middleware(socket, r));
	assert.equal(calls.length, 1, "the failed auth attempt still ran once");
	assert.equal(middleware.cache.size, 0, "a failed auth must not be cached as good");
	assert.ok(err instanceof Error);
	assert.match(err.message, /Unauthorized/);
});

// --- TTL + LRU -------------------------------------------------------------- #
test("an entry expires after its TTL: the next connection re-authenticates", async () => {
	const calls = [];
	const identity = { user: "leader@flock.os", user_type: "System User", sid: "S", authorization_header: undefined };
	const middleware = wrap(fakeFrappeAuth(identity, calls), { ttlMs: 25 });

	await new Promise((r) => middleware(fakeSocket({ cookie: "sid=S" }), r));
	assert.equal(calls.length, 1);
	await sleep(40); // past TTL
	await new Promise((r) => middleware(fakeSocket({ cookie: "sid=S" }), r));
	assert.equal(calls.length, 2, "expired entry -> miss -> frappe authenticate re-runs");
	assert.equal(middleware.cache.stats.misses, 2);
});

test("LRU evicts the least-recently-used sid at the cap", () => {
	const cache = new AuthCache({ ttlMs: 60_000, max: 2 });
	cache.set("a", { user: "u-a", user_type: "X", sid: "a", authorization_header: undefined });
	cache.set("b", { user: "u-b", user_type: "X", sid: "b", authorization_header: undefined });
	// Touch "a" so "b" becomes the eviction candidate (LRU).
	assert.equal(cache.get("a").user, "u-a");
	cache.set("c", { user: "u-c", user_type: "X", sid: "c", authorization_header: undefined });

	assert.equal(cache.size, 2);
	assert.equal(cache.get("a").user, "u-a", "recently-used 'a' survives");
	assert.equal(cache.get("c").user, "u-c", "newly-inserted 'c' survives");
	assert.equal(cache.get("b"), undefined, "least-recently-used 'b' evicted");
	assert.equal(cache.stats.evictions, 1);
});

test("AuthCache get() treats an expired entry as a miss without counting a hit", () => {
	const cache = new AuthCache({ ttlMs: 1, max: 10 });
	cache.set("x", { user: "u", user_type: "X", sid: "x", authorization_header: undefined });
	// Force expiry by rewinding the stored expiry into the past.
	const entry = cache._entries.get("x");
	entry.exp = Date.now() - 1;
	assert.equal(cache.get("x"), undefined);
	assert.equal(cache.stats.hits, 0, "an expired entry is not a hit");
	assert.equal(cache.size, 0, "expired entry is dropped on read");
});

test("defaults are the documented conservative values", () => {
	assert.equal(DEFAULT_TTL_MS, 60_000);
	assert.equal(DEFAULT_MAX, 50_000);
});
