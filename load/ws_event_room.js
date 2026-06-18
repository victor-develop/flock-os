// Flock OS – websocket event-room smoke (FLO-49 / FLO-10 §5.1, §8).
//
// Stands up WS_VUS (default 15,000) concurrent websocket clients on a sharded
// event room. Each client joins exactly ONE presence shard (crc32(ref) % N, the
// same assignment the Python projector uses) PLUS the shared broadcast room,
// then measures connect + first-broadcast latency.
//
// Frappe realtime speaks Socket.IO over engine.io v4 on the socketio server.
// k6 has no built-in Socket.IO client, so this speaks the EIO=4 / SIO=5 wire
// protocol over a raw ws frame (k6/ws): CONNECT the namespace, then emit the
// Frappe `join` event for each room. No bespoke Redis client is used here —
// the broadcast PRODUCER is driven through Frappe (see load/README.md -> WS).
//
//   Scaled-down local smoke:
//     k6 run -e WS_VUS=200 -e WS_DURATION_SEC=30 ws_event_room.js
//   Full acceptance bar:
//     k6 run -e WS_VUS=15000 -e WS_DURATION_SEC=120 ws_event_room.js
//     (then drive the broadcast producer per the README while clients are up)
import ws from "k6/ws";
import { check } from "k6";
import { Counter, Trend } from "k6/metrics";

import { ws as cfg } from "./config.js";
import { roomsFor } from "./lib/shards.js";

// ws_connect_duration: time to open + EIO/Socket.IO CONNECT the namespace.
const connectDuration = new Trend("flock_ws_connect_duration", true);
// flock_ws_broadcast_latency: producer publish -> client receipt (ms).
const broadcastLatency = new Trend("flock_ws_broadcast_latency", true);
const roomsJoined = new Counter("flock_ws_rooms_joined");
const receiveErrors = new Counter("flock_ws_receive_errors");

// Socket.IO over engine.io v4 packet codes (EIO=4 / SIO=5).
const EIO_OPEN = "0";
const SIO_CONNECT = "40";
const sioEvent = (name, data) => `42${JSON.stringify([name, data])}`;

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

export default function () {
	// Stable attendee identity for this VU -> deterministic shard.
	const attendeeRef = `${cfg.eventId}-attendee-${__VU}`;
	const rooms = roomsFor(cfg.eventId, attendeeRef, cfg.shardCount);

	const started = Date.now();
	const res = ws.connect(
		`${cfg.baseUrl}${cfg.socketioPath}/?EIO=4&transport=websocket`,
		{},
		(socket) => {
			socket.on("open", () => {
				// Wait for the engine.io OPEN packet in on('message') then CONNECT.
			});

			socket.on("message", (data) => {
				// engine.io OPEN -> connect the default Socket.IO namespace.
				if (data.startsWith(EIO_OPEN)) {
					socket.send(SIO_CONNECT);
					return;
				}
				// Socket.IO CONNECT ack ("40" / "40{...}") -> join both rooms.
				if (data.startsWith(SIO_CONNECT)) {
					for (const room of rooms) {
						// Frappe socketio server understands the `join` event with a room.
						socket.send(sioEvent("join", { room }));
						roomsJoined.add(1);
					}
					connectDuration.add(Date.now() - started);
					return;
				}
				// Socket.IO EVENT ("42[...]") — a broadcast to a joined room.
				if (data.startsWith("42")) {
					try {
						const [_code, payload] = JSON.parse(data.slice(1));
						const ts = payload && (payload.ts || (payload.payload && payload.payload.ts));
						if (ts) {
							broadcastLatency.add(Date.now() - Number(ts));
						}
					} catch {
						receiveErrors.add(1);
					}
				}
			});

			socket.on("error", () => receiveErrors.add(1));
		},
	);

	check(res, {
		"ws session connected": (r) => r && r.status === 101,
	});
}
