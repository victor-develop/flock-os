// Flock OS – websocket event-room smoke (FLO-49 / FLO-10 §5.1, §8).
//
// Stands up WS_VUS (default 15,000) concurrent websocket clients on a sharded
// event room. Each client joins exactly ONE presence shard (crc32(ref) % N, the
// same assignment the Python projector uses) PLUS the shared broadcast room,
// then measures connect + first-broadcast latency.
//
// Frappe realtime speaks Socket.IO over engine.io v4 on the node socketio
// server (`apps/frappe/realtime/index.js`). k6 has no built-in Socket.IO client,
// so this speaks the EIO=4 / SIO=5 wire protocol over a raw ws frame (k6/ws):
// CONNECT the per-site namespace, emit the `join` event for each room, then
// answer engine.io pings. No bespoke Redis client is used here — the broadcast
// PRODUCER is driven through Frappe (see load/README.md -> WS).
//
// Frappe v15 namespaces per site: `io.of(/^\/.*$/)` + a namespace auth
// middleware that requires a `sid` cookie / Authorization header. The projector
// publishes to `/<site>`, so the smoke must CONNECT `/<site>` AND authenticate
// (FLO-107 — previously it CONNECTed the default `/` namespace with no auth, so
// published broadcasts never overlapped with the clients' room universe).
//
//   Scaled-down local smoke:
//     k6 run -e WS_VUS=200 -e WS_DURATION_SEC=30 ws_event_room.js
//   Full acceptance bar:
//     k6 run -e WS_VUS=15000 -e WS_DURATION_SEC=120 ws_event_room.js
//     (then drive the broadcast producer per the README while clients are up)
import ws from "k6/ws";
import { check } from "k6";
import { Counter, Trend } from "k6/metrics";

import { ws as cfg, write } from "./config.js";
import { roomsFor } from "./lib/shards.js";
import { loginSid } from "./lib/auth.js";

// ws_connect_duration: time to open + EIO/Socket.IO CONNECT the namespace.
const connectDuration = new Trend("flock_ws_connect_duration", true);
// flock_ws_broadcast_latency: producer publish -> client receipt (ms).
const broadcastLatency = new Trend("flock_ws_broadcast_latency", true);
const roomsJoined = new Counter("flock_ws_rooms_joined");
// flock_ws_receive_errors: in-session protocol errors (establishment failure,
// a malformed broadcast EVENT, or a namespace/auth rejection). Thresholded at 0
// — a non-zero count is a real bug (e.g. the FLO-105 slice bug that swallowed
// every broadcast, or the FLO-107 namespace/auth gap that rejected every join).
const receiveErrors = new Counter("flock_ws_receive_errors");
// flock_ws_teardown_errors: socket errors from forced teardown at gracefulStop /
// ramp-down. NOT in-session errors — k6 closes established sockets at stage
// transitions, so these are expected and not thresholded (FLO-105).
const teardownErrors = new Counter("flock_ws_teardown_errors");

// engine.io v4 packet codes (server frames): 0=open, 2=ping, 4=socket.io msg.
const EIO_OPEN = "0";
const EIO_PING = "2";
// Socket.IO v5 packet codes ride inside engine.io message ("4"):
//   "40" CONNECT, "42" EVENT, "44" ERROR — each optionally namespaced.
const SIO_CONNECT = "40";
const SIO_ERROR = "44";

// Per-site namespace prefix. Frappe realtime serves `io.of(/^\/.*$/)` and the
// projector publishes to `/<site>`; every non-default Socket.IO packet carries
// the namespace followed by a comma (socket.io-parser: `nsp + ","`). For site
// `flock_os.localhost`:
//   CONNECT: `40/flock_os.localhost,`
//   EVENT:   `42/flock_os.localhost,["join",{...}]`
// For the default `/` namespace the prefix is empty (no comma), so this stays
// compatible with a single-site/dev override (FLO-107).
const nspPrefix = cfg.site ? `/${cfg.site},` : "";
const sioConnectPacket = `40${nspPrefix}`; // CONNECT the /<site> namespace
const sioEventPacket = (name, data) => `42${nspPrefix}${JSON.stringify([name, data])}`;

// Decode a Socket.IO EVENT ("42[nsp,]..."): strip the "42" type + optional
// "/<namespace>," prefix, leaving the JSON array `[event, payload]`. Handles
// both the default namespace (`42[...]`) and namespaced frames
// (`42/<site>,[...]`) — the FLO-105 fix only sliced 1 char, which left invalid
// JSON under a namespaced frame (FLO-107).
function parseEvent(data) {
	let json = data.slice(2); // drop "42"
	if (json.startsWith("/")) {
		// namespaced frame: "/<site>,[...]" → drop through the comma
		const comma = json.indexOf(",");
		if (comma >= 0) json = json.slice(comma + 1);
	}
	return JSON.parse(json);
}

export const options = {
	thresholds: {
		// §8: ws broadcast < 1s; connect should be well under that at 15k scale.
		flock_ws_connect_duration: [`p(95)<${Math.min(cfg.broadcastBudgetMillis, 1000)}`],
		flock_ws_broadcast_latency: [`p(95)<${cfg.broadcastBudgetMillis}`],
		flock_ws_receive_errors: ["count==0"],
	},
	scenarios: {
		sharded_room: {
			executor: "ramping-vus",
			startVUs: 0,
			stages: [
				{ target: cfg.vus, duration: "60s" }, // ramp to 15k over 60s
				{ target: cfg.vus, duration: `${cfg.durationSec}s` },
				{ target: 0, duration: "30s" },
			],
			gracefulStop: "30s",
		},
	},
};

// Log in ONCE (shared across every VU) so the full 15k bar does not issue 15k
// logins — each VU presents the same session `sid` to the realtime namespace
// auth middleware. setup() runs in the k6 init context; its return value is
// passed to default(data). Login hits the web (gunicorn) port, not the ws port.
export function setup() {
	const sid = loginSid(write.baseUrl, cfg.username, cfg.password);
	return { sid };
}

export default function ({ sid }) {
	// Stable attendee identity for this VU -> deterministic shard.
	const attendeeRef = `${cfg.eventId}-attendee-${__VU}`;
	const rooms = roomsFor(cfg.eventId, attendeeRef, cfg.shardCount);

	const started = Date.now();
	const res = ws.connect(
		`${cfg.baseUrl}${cfg.socketioPath}/?EIO=4&transport=websocket`,
		{
			// Realtime namespace auth (authenticate.js): requires a `sid` cookie
			// or Authorization header, and a Host/Origin hostname match. The
			// Cookie header is what lets the socket pass the per-site namespace
			// gate and reach the flock_os room-join handler (FLO-107).
			headers: {
				Cookie: `sid=${sid}`,
				Origin: cfg.origin,
			},
		},
		(socket) => {
			// Tracks whether this socket has established + subscribed. Socket
			// errors after join are teardown (expected); before join they are
			// establishment failures (gate-relevant). FLO-105.
			let joined = false;

			socket.on("message", (data) => {
				// engine.io OPEN ("0{...}") → CONNECT the per-site namespace.
				if (data.startsWith(EIO_OPEN)) {
					socket.send(sioConnectPacket);
					return;
				}
				// engine.io PING ("2") → answer PONG ("3") so the server's
				// heartbeat check keeps the socket alive through the 120s bar.
				if (data.charCodeAt(0) === 50 /* "2" */) {
					socket.send("3");
					return;
				}
				// Socket.IO CONNECT ack ("40" / "40/<site>,{...}") → join rooms.
				if (data.startsWith(SIO_CONNECT)) {
					for (const room of rooms) {
						// flock_os room-join handler: socket.on("join", {room}).
						socket.send(sioEventPacket("join", { room }));
						roomsJoined.add(1);
					}
					joined = true;
					connectDuration.add(Date.now() - started);
					return;
				}
				// Socket.IO ERROR ("44/<ns>,{...}") → namespace rejected (bad
				// auth/origin). A non-zero receive-error count surfaces it in the
				// gate instead of a silent zero-broadcast run (FLO-107).
				if (data.startsWith(SIO_ERROR)) {
					receiveErrors.add(1);
					return;
				}
				// Socket.IO EVENT ("42[...]") — a broadcast to a joined room.
				if (data.startsWith("42")) {
					try {
						const [_code, payload] = parseEvent(data);
						const ts = payload && (payload.ts || (payload.payload && payload.payload.ts));
						if (ts) {
							broadcastLatency.add(Date.now() - Number(ts));
						}
					} catch {
						receiveErrors.add(1);
					}
				}
			});

			socket.on("error", () => {
				// Pre-join error = establishment failure (gate-relevant). A socket
				// that already joined is being torn down by k6 at gracefulStop /
				// ramp-down — expected, route to the separate counter (FLO-105).
				if (joined) {
					teardownErrors.add(1);
				} else {
					receiveErrors.add(1);
				}
			});
		},
	);

	check(res, {
		"ws session connected": (r) => r && r.status === 101,
	});
}
