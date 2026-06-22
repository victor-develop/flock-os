// Unit tests for the flock_os realtime room-join handler (FLO-107 + FLO-106).
//
// Runs under plain `node --test` — no bench, no socket.io, no Frappe — because
// the handler's room ACL + branch-scope allow/deny branching is pure once the
// authorizer is injected. Mirrors the shard-parity test's approach (load/lib).
//
//   node --test realtime/handlers/flock_room_handlers.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";

import {
	isFlockEventRoom,
	makeJoinListener,
	defaultAuthorize,
	scopeCacheGet,
	scopeCacheSet,
	FLOCK_EVENT_ROOM_RE,
	FLOCK_SCOPE_ENDPOINT,
} from "./flock_room_handlers.js";

// Minimal fake socket: records every room it joined / left.
function fakeSocket() {
	const joined = [];
	return { joined, join: (r) => joined.push(r) };
}

const allow = async () => true;
const deny = async () => false;

// --- gate 1: room ACL ------------------------------------------------------ #
test("isFlockEventRoom matches broadcast + shard rooms only", () => {
	for (const room of [
		"flock_os:event:gathering-smoke:broadcast",
		"flock_os:event:g1:shard:0",
		"flock_os:event:hyphen-id-1:shard:9",
	]) {
		assert.ok(isFlockEventRoom(room), `match ${room}`);
	}
});

test("isFlockEventRoom rejects non-flock / malformed / Frappe-internal rooms", () => {
	for (const room of [
		"",
		"doc:Flock Gathering/x", // a Frappe doc room — never route here
		"doctype:Flock Gathering",
		"user:leader@flock.os",
		"flock_os:event:no-segment",
		"flock_os:event:g1:shard",
		"flock_os:event:g1:shard:-1",
		"flock_os:event:g1:whitelist",
		"other-app:event:g1:broadcast",
		null,
		123,
		"flock_os:event::broadcast",
	]) {
		assert.ok(!isFlockEventRoom(room), `reject ${JSON.stringify(room)}`);
	}
});

test("FLOCK_EVENT_ROOM_RE: non-greedy event id cannot spoof the tail", () => {
	assert.ok(isFlockEventRoom("flock_os:event:evil:broadcast"));
});

// --- gate 2: branch-scope authorize --------------------------------------- #
test("makeJoinListener joins when authorize allows", async () => {
	const socket = fakeSocket();
	const onJoin = makeJoinListener(socket, allow);
	await onJoin({ room: "flock_os:event:g1:broadcast" });
	await onJoin({ room: "flock_os:event:g1:shard:3" });
	assert.deepEqual(socket.joined, ["flock_os:event:g1:broadcast", "flock_os:event:g1:shard:3"]);
});

test("makeJoinListener does NOT join when authorize denies (out-of-scope branch)", async () => {
	const socket = fakeSocket();
	await makeJoinListener(socket, deny)({ room: "flock_os:event:g1:broadcast" });
	assert.deepEqual(socket.joined, []);
});

test("makeJoinListener skips the scope check for non-flock rooms", async () => {
	const socket = fakeSocket();
	let called = 0;
	const authorize = async () => {
		called++;
		return true;
	};
	await makeJoinListener(socket, authorize)({ room: "doc:Flock Gathering/x" });
	await makeJoinListener(socket, authorize)({ room: "flock_os:event:g1:bad" });
	assert.equal(called, 0, "authorize must not run for non-flock rooms");
	assert.deepEqual(socket.joined, []);
});

test("makeJoinListener accepts a bare-string join payload too", async () => {
	const socket = fakeSocket();
	await makeJoinListener(socket, allow)("flock_os:event:g1:shard:2");
	assert.deepEqual(socket.joined, ["flock_os:event:g1:shard:2"]);
});

test("makeJoinListener never throws on authorize rejection (best-effort)", async () => {
	const socket = fakeSocket();
	const failing = async () => {
		throw new Error("scope check HTTP 500");
	};
	await assert.doesNotReject(
		makeJoinListener(socket, failing)({ room: "flock_os:event:g1:broadcast" }),
	);
	assert.deepEqual(socket.joined, []);
});

// --- defaultAuthorize: HTTP scope-check shape (frappe_request injected) ---- #
// A fake frappe_request returning a superagent-like chainable. Captures the
// endpoint path so the test pins the bench-only `flock_os.realtime_views`
// surface (FLO-112 — `realtime.py` is import-clean, so the
// `@frappe.whitelist()` decorator lives in `realtime_views`, not `realtime`).
function fakeFrappeRequest(outcome, captured = {}) {
	return (path, _socket) => {
		captured.path = path;
		const chain = {
			type() {
				return chain;
			},
			query(params) {
				captured.query = params;
				return chain;
			},
			end(cb) {
				if (outcome.error) return cb(outcome.error);
				cb(null, { status: outcome.status, body: outcome.body });
			},
		};
		return chain;
	};
}

test("defaultAuthorize POSTs the bench-only realtime_views scope endpoint", async () => {
	const captured = {};
	const auth = defaultAuthorize({}, fakeFrappeRequest({ status: 200, body: { message: true } }, captured));
	await auth("flock_os:event:g1:broadcast");
	assert.equal(captured.path, FLOCK_SCOPE_ENDPOINT);
	assert.equal(
		captured.path,
		"/api/method/flock_os.realtime_views.can_join_event_room",
		"must hit the whitelisted realtime_views surface, not realtime (import-clean)",
	);
});

test("defaultAuthorize resolves true on HTTP 200 with truthy message", async () => {
	const auth = defaultAuthorize({}, fakeFrappeRequest({ status: 200, body: { message: true } }));
	assert.equal(await auth("flock_os:event:g1:broadcast"), true);
});

test("defaultAuthorize resolves false on 403 / message:false / network error", async () => {
	for (const outcome of [
		{ status: 403, body: { message: false } },
		{ status: 200, body: { message: false } },
		{ status: 500, body: {} },
		{ error: new Error("ECONNREFUSED") },
	]) {
		const auth = defaultAuthorize({}, fakeFrappeRequest(outcome));
		assert.equal(await auth("flock_os:event:g1:broadcast"), false);
	}
});

// --- FLO-815: per-socket throttle key passthrough ------------------------- #
test("defaultAuthorize forwards socket.id as socket_id for per-socket throttle (FLO-815)", async () => {
	const captured = {};
	const socket = { id: "flock_os.localhost#abc-123-socket" };
	const auth = defaultAuthorize(socket, fakeFrappeRequest({ status: 200, body: { message: true } }, captured));
	await auth("flock_os:event:g1:broadcast");
	assert.deepEqual(captured.query, {
		room: "flock_os:event:g1:broadcast",
		socket_id: "flock_os.localhost#abc-123-socket",
	});
});

// --- FLO-815: per-session scope-check cache ------------------------------- #
// At the 15k bar every client shares one sid; the scope decision per (sid, room)
// is identical. The cache collapses 30k HTTP callbacks to ~11 per worker.
const ROOM_B = "flock_os:event:g1:broadcast";
const ROOM_S = "flock_os:event:g1:shard:3";

test("scope cache miss returns undefined (first call per sid+room)", () => {
	assert.equal(scopeCacheGet("sid-cache-test", ROOM_B), undefined);
});

test("scope cache stores and replays a successful allow decision", () => {
	scopeCacheSet("sid-allow", ROOM_B, true);
	assert.equal(scopeCacheGet("sid-allow", ROOM_B), true);
});

test("scope cache stores and replays a deny decision", () => {
	scopeCacheSet("sid-deny", ROOM_S, false);
	assert.equal(scopeCacheGet("sid-deny", ROOM_S), false);
});

test("scope cache keys are independent per room", () => {
	scopeCacheSet("sid-mixed", ROOM_B, true);
	scopeCacheSet("sid-mixed", ROOM_S, false);
	assert.equal(scopeCacheGet("sid-mixed", ROOM_B), true);
	assert.equal(scopeCacheGet("sid-mixed", ROOM_S), false);
});

test("scope cache keys are independent per sid", () => {
	scopeCacheSet("sid-a", ROOM_B, true);
	scopeCacheSet("sid-b", ROOM_B, false);
	assert.equal(scopeCacheGet("sid-a", ROOM_B), true);
	assert.equal(scopeCacheGet("sid-b", ROOM_B), false);
});

test("defaultAuthorize serves a cache hit without calling frappe_request", async () => {
	const sid = "sid-hit-test";
	scopeCacheSet(sid, ROOM_B, true);
	let httpCalled = false;
	const socket = { id: "s1", sid };
	const auth = defaultAuthorize(socket, () => {
		httpCalled = true;
		return fakeFrappeRequest({ status: 200, body: { message: true } })();
	});
	const result = await auth(ROOM_B);
	assert.equal(result, true);
	assert.equal(httpCalled, false, "cache hit must NOT call frappe_request");
});

test("defaultAuthorize caches a 200 allow for subsequent sockets with same sid", async () => {
	const sid = "sid-cache-after-200";
	let callCount = 0;
	const makeReq = () => {
		callCount++;
		return fakeFrappeRequest({ status: 200, body: { message: true } })();
	};
	// First socket: miss → HTTP → cache.
	const auth1 = defaultAuthorize({ id: "s1", sid }, makeReq);
	await auth1(ROOM_B);
	// Second socket: same sid → cache hit, no HTTP.
	const auth2 = defaultAuthorize({ id: "s2", sid }, makeReq);
	await auth2(ROOM_B);
	assert.equal(callCount, 1, "second socket with same sid must hit cache");
});

test("defaultAuthorize does NOT cache error/timeout responses", async () => {
	const sid = "sid-no-cache-error";
	const outcomes = [
		{ error: new Error("ECONNREFUSED") },
		{ status: 200, body: { message: true } },
	];
	let i = 0;
	const makeReq = () => fakeFrappeRequest(outcomes[i++])();
	// First call: error → resolve(false), NOT cached.
	const auth1 = defaultAuthorize({ id: "s1", sid }, makeReq);
	assert.equal(await auth1(ROOM_B), false);
	// Second call: should NOT be cached → hits HTTP → 200 → true.
	const auth2 = defaultAuthorize({ id: "s2", sid }, makeReq);
	assert.equal(await auth2(ROOM_B), true);
});
