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
			query() {
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

// --- FLO-428 edge-case regressions ----------------------------------------- #
// Audit: realtime engagement tier authorization edge cases at 15k scale. The
// audit found NO bugs in the join handler — these tests freeze the safe
// behavior so a future refactor cannot silently regress it. The companion
// narrative lives in docs/operations/realtime-edge-cases.md.

// A Socket.IO-shaped fake: `rooms` is a SET (mirrors real socket.io behavior —
// joining the same room twice is a no-op). The reconnect-storm idempotency
// proof depends on this set semantics, not the array fakeSocket() above.
function setSocket() {
	const rooms = new Set();
	return {
		rooms,
		join(r) {
			rooms.add(r);
		},
		leave(r) {
			rooms.delete(r);
		},
	};
}

// Edge case 4 — reconnect storm: re-joins MUST be idempotent (no duplicate
// room subscriptions, no leak). Socket.IO room membership is a set; the flock_os
// listener calls socket.join(room) straight through on every `join` event, so
// the dedup is socket.io's job. This pins the listener never defeats that.
test("FLO-428 #4 reconnect storm: re-joining the same room N times leaves the socket in it exactly once", async () => {
	const socket = setSocket();
	const onJoin = makeJoinListener(socket, allow);
	const room = "flock_os:event:g1:shard:3";
	// A reconnect storm: the client re-emits `join` for the same shard many
	// times (browser retry, worker restart redelivery, etc).
	for (let i = 0; i < 1000; i++) {
		await onJoin({ room });
	}
	assert.equal(socket.rooms.size, 1, "no duplicate room subscriptions");
	assert.ok(socket.rooms.has(room));
});

test("FLO-428 #4 reconnect storm: shard + broadcast re-joins land in a 2-room set", async () => {
	const socket = setSocket();
	const onJoin = makeJoinListener(socket, allow);
	const shard = "flock_os:event:g1:shard:4";
	const broadcast = "flock_os:event:g1:broadcast";
	// Each reconnect emits both joins; the storm repeats both 500 times.
	for (let i = 0; i < 500; i++) {
		await onJoin({ room: shard });
		await onJoin({ room: broadcast });
	}
	assert.equal(socket.rooms.size, 2, "exactly the two distinct rooms");
	assert.ok(socket.rooms.has(shard));
	assert.ok(socket.rooms.has(broadcast));
});

test("FLO-428 #4 reconnect storm: leave between re-joins cannot leak stale rooms", async () => {
	// The sequence a buggy client might send on reconnect: leave + re-join the
	// same room. The listener handles leave and join independently (no shared
	// state), so the end state is exactly the post-join room set.
	const socket = setSocket();
	const onJoin = makeJoinListener(socket, allow);
	const room = "flock_os:event:g1:broadcast";
	await onJoin({ room });
	socket.leave(room);
	assert.equal(socket.rooms.size, 0, "left cleanly");
	await onJoin({ room });
	assert.equal(socket.rooms.size, 1, "re-joined cleanly");
});

// Edge case 1 — revoked ticket on join: the per-join scope check is INDEPENDENT
// of the connect-time auth cache. Whether the cache served a HIT or a MISS for
// the socket's connect, the join handler runs the live HTTP scope check through
// `defaultAuthorize`. If the gathering's branch is revoked (scope endpoint
// returns false / errors), the socket stays out of the room — no stale-trust.
test("FLO-428 #1 revoked ticket on join: a denied scope check keeps the socket out even after a prior allow", async () => {
	const socket = setSocket();
	// First join: scope allows.
	const allowAuth = async () => true;
	await makeJoinListener(socket, allowAuth)({ room: "flock_os:event:g1:broadcast" });
	assert.equal(socket.rooms.size, 1);
	// Simulate mid-burst revocation: the scope endpoint now denies (the user's
	// branch permission was stripped). The socket already in the broadcast room
	// is not forcibly evicted (best-effort realtime, FLO-10 §5.3), BUT a fresh
	// `join` for a DIFFERENT room must not be granted.
	const denyAuth = async () => false;
	const shardSocket = setSocket();
	await makeJoinListener(shardSocket, denyAuth)({ room: "flock_os:event:g1:shard:2" });
	assert.equal(shardSocket.rooms.size, 0, "revoked scope must not allow new joins");
});

test("FLO-428 #1 revoked ticket on join: scope endpoint error (500/ECONNREFUSED) fails closed", async () => {
	// Same as above but the revocation surfaces as a transport error during
	// the burst (e.g. gunicorn throttled) — the listener must still keep the
	// socket out, never let a network blip open an unscoped subscription.
	for (const outcome of [
		{ status: 500, body: {} },
		{ status: 502, body: {} },
		{ error: new Error("ECONNREFUSED") },
		{ error: new Error("ETIMEDOUT") },
	]) {
		const socket = setSocket();
		const auth = defaultAuthorize({}, fakeFrappeRequest(outcome));
		await makeJoinListener(socket, auth)({ room: "flock_os:event:g1:broadcast" });
		assert.equal(socket.rooms.size, 0, `revocation-as-error must deny: ${JSON.stringify(outcome)}`);
	}
});

test("FLO-428 #1 revoked ticket on join: scope check fires on EVERY join event (no per-room memoization)", async () => {
	// Pins that the join handler does NOT cache the scope decision per room —
	// every `join` event triggers a fresh `authorize` call. This is what makes
	// revocation effective mid-burst: the next join attempt re-checks live.
	let calls = 0;
	const countingAuth = async () => {
		calls++;
		return true;
	};
	const socket = setSocket();
	const onJoin = makeJoinListener(socket, countingAuth);
	for (let i = 0; i < 50; i++) {
		await onJoin({ room: "flock_os:event:g1:broadcast" });
	}
	assert.equal(calls, 50, "scope check is live per join, never memoized");
});
