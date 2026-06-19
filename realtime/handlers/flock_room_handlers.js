// flock_os realtime room-join handler (FLO-107 / FLO-10 §5.1).
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
// idempotent `register(socket)` call into the server's `on_connection`. The
// logic stays in flock_os; only a single guarded require lands in vendored
// Frappe. See `docs/development/ws-broadcast-delivery.md`.
//
// Room-join event shape — the §5.1 client→server contract:
//
//   client emits:  42/<site>,["join",{"room":"flock_os:event:<id>:..."}]
//   server does:   socket.join("flock_os:event:<id>:...")
//
// ACL: only `flock_os:event:`-prefixed rooms are joinable here, so an
// authenticated socket cannot eavesdrop on Frappe's internal rooms (`doc:*`,
// `task:*`, `user:*`, …). Membership-aware per-event/per-branch scoping is a
// follow-up for the §5.1 ownership projector, not this smoke-unblocker.
//
// `socket` is the already-authenticated Socket.IO socket for the per-site
// namespace (authenticate.js has populated `socket.user`).

const FLOCK_EVENT_ROOM_PREFIX = "flock_os:event:";

function flock_room_handlers(socket) {
	// Accept both the object shape emitted by the k6 smoke + desk client
	// (`["join", {room}]`) and the legacy bare-string shape (`["join", room]`)
	// so every flock_os client shares one join path.
	socket.on("join", function (data) {
		const room = typeof data === "string" ? data : data && data.room;
		if (!room || !room.startsWith(FLOCK_EVENT_ROOM_PREFIX)) {
			return; // not a flock_os event room — refuse (ACL).
		}
		socket.join(room);
	});

	socket.on("leave", function (data) {
		const room = typeof data === "string" ? data : data && data.room;
		if (!room || !room.startsWith(FLOCK_EVENT_ROOM_PREFIX)) {
			return;
		}
		socket.leave(room);
	});
}

module.exports = flock_room_handlers;
