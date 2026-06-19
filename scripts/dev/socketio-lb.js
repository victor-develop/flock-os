#!/usr/bin/env node
//
// flock_os scaled-socketio load balancer (FLO-121 / FLO-10 §8).
//
// A dependency-free, pure-node TCP round-robin proxy that distributes incoming
// WebSocket connections across N socketio backend processes. It is the local
// horizontal-scaling tier for the §8 15k WS connection-setup wall: one node
// event loop serializes ~15k concurrent handshakes (connect p95 ~27 s, <1 %
// established), so running N backends behind this LB spreads the handshakes and
// collapses the wall.
//
// Why a *TCP* (L4) round-robin is correct here, not sticky L7:
//   The k6 smoke (load/ws_event_room.js) uses `transport=websocket` ONLY — no
//   engine.io polling upgrade — so each client is exactly ONE independent TCP
//   connection whose full lifecycle (EIO OPEN, SIO CONNECT, JOIN, broadcasts,
//   pings) stays on whichever backend it first lands on. There is no separate
//   polling handshake that must revisit the same backend, so no sticky session is
//   needed: plain per-connection round-robin distributes cleanly and each
//   socket's state is self-contained on its backend. Cross-backend fan-out is
//   handled by `@socket.io/redis-adapter` (wired by wire-socketio-redis-adapter.sh)
//   + the existing replicated "events" pub/sub subscriber.
//
// The proxy pipes the raw byte stream both directions with no parsing, so the
// backend's realtime auth (Host/Origin/sid) sees the real client handshake
// untouched. socket.handshake.address becomes the LB's IP, which does not affect
// Frappe auth (sid cookie + hostname match only). For a polling/mixed-transport
// tier (not this smoke) you would need sticky L7 (nginx ip_hash/cookie) instead.
//
// Usage:
//   PORT=9000 BACKENDS=127.0.0.1:9001,127.0.0.1:9002,127.0.0.1:9003 node scripts/dev/socketio-lb.js
//   STATS_INTERVAL_MS=5000   # per-backend connection counters to stderr (0 = off)
//
// Runbook: docs/development/ws-broadcast-delivery.md -> Scaling the socketio tier.
"use strict";

const net = require("node:net");

const PORT = parseInt(process.env.PORT || "9000", 10);
const STATS_INTERVAL_MS = parseInt(process.env.STATS_INTERVAL_MS || "0", 10);
const BACKENDS = (process.env.BACKENDS || "")
	.split(",")
	.map((s) => s.trim())
	.filter(Boolean);

if (BACKENDS.length === 0) {
	console.error("socketio-lb: BACKENDS env required (comma-separated host:port list)");
	process.exit(2);
}

// Per-backend counters so a scale run can verify connections actually distributed
// across the tier (the whole point of horizontal scaling).
const stats = BACKENDS.map((b) => ({ backend: b, accepted: 0, active: 0, failed: 0 }));
let rr = 0; // round-robin cursor

function nextBackend() {
	const idx = rr % BACKENDS.length;
	rr++;
	return { idx, hostport: BACKENDS[idx] };
}

const server = net.createServer((client) => {
	const { idx, hostport } = nextBackend();
	const [host, port] = hostport.split(":");
	const upstream = net.connect({ host: host || "127.0.0.1", port: parseInt(port, 10) });

	const onUpErr = (err) => {
		stats[idx].failed++;
		// ECONNREFUSED during startup ramp is expected; surface once for ops.
		console.error(`socketio-lb: backend ${hostport} connect error: ${(err && err.code) || err}`);
		client.destroy();
	};
	upstream.on("error", onUpErr);

	upstream.on("connect", () => {
		stats[idx].accepted++;
		stats[idx].active++;
		// Bidirectional pipe: raw bytes flow untouched both ways. Destroying one
		// side tears down the other so a dropped WS closes cleanly on both ends.
		client.pipe(upstream);
		upstream.pipe(client);
	});

	const cleanup = () => {
		if (stats[idx].active > 0) stats[idx].active--;
	};
	client.on("close", cleanup);
	upstream.on("close", cleanup);
	// If the client errors before upstream connects, drop the half-open upstream.
	client.on("error", () => {
		upstream.destroy();
		client.destroy();
	});
});

server.on("error", (err) => {
	if (err.code === "EADDRINUSE") {
		console.error(`socketio-lb: port ${PORT} in use — stop the single-process socketio first (scripts/dev/scale-socketio.sh stop).`);
	} else {
		console.error(`socketio-lb: server error: ${(err && err.message) || err}`);
	}
	process.exit(1);
});

server.listen(PORT, "0.0.0.0", () => {
	console.log(`socketio-lb: listening on :${PORT}, round-robin across ${BACKENDS.length} backend(s):`);
	BACKENDS.forEach((b, i) => console.log(`  [${i}] ${b}`));
});

if (STATS_INTERVAL_MS > 0) {
	const timer = setInterval(() => {
		const line = stats
			.map((s) => `${s.backend}(acc=${s.accepted},act=${s.active},fail=${s.failed})`)
			.join(" ");
		console.log(`socketio-lb stats: ${line}`);
	}, STATS_INTERVAL_MS);
	timer.unref();
}

// Graceful shutdown so the scale orchestrator's `kill` leaves no half-open TCP.
function shutdown() {
	server.close(() => process.exit(0));
	// Force-exit after a short grace if lingering sockets delay close.
	setTimeout(() => process.exit(0), 1000).unref();
}
process.on("SIGTERM", shutdown);
process.on("SIGINT", shutdown);
