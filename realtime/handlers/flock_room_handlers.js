// flock_os realtime room-join handler (FLO-107 / FLO-106 / FLO-10 §5.1).
//
// Frappe v15's realtime server (`apps/frappe/realtime/index.js`) auto-joins only
// `user:<u>`, `website`, and (system users) `all` — its `frappe_handlers.js` has
// no generic `join` event, and Frappe offers no per-app handler loader. So
// flock_os's §5.1 sharded rooms (`flock_os:event:<id>:shard:N` + `:broadcast`)
// have no server-side way for a client to subscribe; the projector's
// `frappe.publish_realtime` fan-out reaches an empty subscriber set (FLO-107
// root cause #2). This module closes that gap.
//
// It is flock_os-owned (version-controlled here) and wired into the bench
// realtime server by `scripts/dev/wire-socketio-handler.sh`, which inserts one
// idempotent `require(...)` call into the server's `on_connection`. The logic
// stays in flock_os; only a single guarded require lands in vendored Frappe.
// See `docs/development/ws-broadcast-delivery.md`.
//
// Room-join event shape — the §5.1 client→server contract:
//
//   client emits:  42/<site>,["join",{"room":"flock_os:event:<id>:..."}]
//   server does:   socket.join("flock_os:event:<id>:...")   iff scope-checked
//
// ACL + scope (two gates, defense in depth):
//   1. Prefix ACL — only `flock_os:event:*` rooms route here, so an authenticated
//      socket cannot eavesdrop on Frappe's internal rooms (`doc:*`, `task:*`,
//      `user:*`, …).
//   2. Branch scope (FLO-106) — before joining, ask
//      `flock_os.realtime.can_join_event_room` whether the socket's user has
//      branch scope over the gathering (ADR §6.2), the same decision that scopes
//      `tabFlock Attendance Record` rows. Fails closed: a denied/errored check
//      keeps the socket out of the room. (FLO-107 shipped gate 1 as the smoke
//      unblocker and deferred gate 2; FLO-106 is that follow-up.)
//
// `socket` is the already-authenticated Socket.IO socket for the per-site
// namespace (authenticate.js has populated `socket.user` / `socket.sid` /
// `socket.authorization_header`, which `frappe_request` carries to the scope
// endpoint so `frappe.session.user` resolves server-side).

const path = require("node:path");

const FLOCK_EVENT_ROOM_PREFIX = "flock_os:event:";

// Mirrors flock_os.realtime.EVENT_ROOM_RE. Pinned by the JS parity test +
// flock_os/tests/test_realtime_room_scope.py. Stricter than the prefix ACL: a
// malformed room (e.g. `flock_os:event:g1:whitelist`) never becomes an HTTP
// scope-check call.
const FLOCK_EVENT_ROOM_RE = /^flock_os:event:.+?:(?:broadcast|shard:\d+)$/;

function isFlockEventRoom(room) {
	return typeof room === "string" && FLOCK_EVENT_ROOM_RE.test(room);
}

// Default branch-scope authorizer: POST flock_os.realtime.can_join_event_room
// over the socket's session (same shape as Frappe's `can_subscribe_doc`). It
// returns true iff the user's branch scope covers the gathering's branch.
//
// `frappeRequest` is injectable so the gate's allow/deny branching is unit-
// testable with no bench; in production the wiring passes frappe's own
// `realtime/utils.js` (`frappe_request`). Resolved lazily as a fallback so the
// module still imports clean under `node --test`.
function defaultAuthorize(socket, frappeRequest) {
	const request = frappeRequest || requireFrappeRequest();
	return async function authorize(room) {
		return new Promise((resolve) => {
			request("/api/method/flock_os.realtime.can_join_event_room", socket)
				.type("form")
				.query({ room })
				.end((err, res) => {
					if (err || !res || res.status !== 200) return resolve(false);
					resolve(Boolean(res.body && res.body.message));
				});
		});
	};
}

// Lazily resolve frappe's realtime/utils (it ships frappe_request). The handler
// runs inside frappe's node process at apps/flock_os/realtime/handlers/, so the
// vendored realtime utils live at ../../../frappe/realtime/utils (the same bench
// layout the wiring script assumes). Best-effort: a wrong path fails closed.
function requireFrappeRequest() {
	const utils = require(path.join(__dirname, "..", "..", "..", "frappe", "realtime", "utils"));
	return utils.frappe_request;
}

// Build the `join` listener for one socket. `authorize(room)` -> Promise<bool>
// is the branch-scope gate. On allow, join; on deny/error, stay out (best-effort
// realtime — FLO-10 §5.3; never kill the socket on a denied join).
function makeJoinListener(socket, authorize) {
	return async (data) => {
		const room = typeof data === "string" ? data : data && data.room;
		if (!isFlockEventRoom(room)) return; // gate 1: prefix/shape ACL
		try {
			if (await authorize(room)) socket.join(room); // gate 2: branch scope
		} catch {
			// denied or errored scope check — stay out of the room
		}
	};
}

// Wired from the bench realtime server's on_connection:
//   require("../../flock_os/realtime/handlers/flock_room_handlers")(socket)
// `deps` is optional: production wiring may pass `{ frappe_request }` from
// frappe's own utils (decoupling this module from frappe's path); tests inject
// `{ authorize }` to exercise the gate with no HTTP.
function flock_room_handlers(socket, deps) {
	const authorize =
		(deps && deps.authorize) || defaultAuthorize(socket, deps && deps.frappe_request);
	socket.on("join", makeJoinListener(socket, authorize));

	// `leave` is the client's own membership — no scope check needed (leaving a
	// room you are not in is a harmless no-op).
	socket.on("leave", function (data) {
		const room = typeof data === "string" ? data : data && data.room;
		if (!isFlockEventRoom(room)) return;
		socket.leave(room);
	});
}

module.exports = flock_room_handlers;
module.exports.isFlockEventRoom = isFlockEventRoom;
module.exports.makeJoinListener = makeJoinListener;
module.exports.defaultAuthorize = defaultAuthorize;
module.exports.FLOCK_EVENT_ROOM_RE = FLOCK_EVENT_ROOM_RE;
