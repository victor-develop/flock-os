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
// A fake frappe_request returning a superagent-like chainable.
function fakeFrappeRequest(outcome) {
	return (_path, _socket) => {
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
