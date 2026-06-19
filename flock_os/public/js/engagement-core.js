/*
 * Flock OS — engagement client core (FLO-12 / FLO-9 §8, §10, §12).
 *
 * The `useEngagementSession(sessionId)` hook + the realtime/shard parity
 * primitives every player experience shares. Responsibilities:
 *
 *   - session ticket handling (join → signed ticket carried on every call),
 *   - WebSocket subscription via Frappe realtime, with short-polling fallback
 *     on `GET …/state` + exponential backoff when the socket drops (§8),
 *   - IndexedDB optimistic offline queue: every interaction is written locally
 *     with a client nonce + the ticket before it hits the network, so the UI
 *     never blocks; a background-sync flush drains the queue on reconnect to the
 *     bulk endpoint (server dedups by nonce + validates the ticket was issued
 *     in-window — §8 / §6.4),
 *   - reconnect/restore: on reconnect the client pulls the last confirmed
 *     server state so a mid-game dropout that already participated keeps its
 *     attendance (§7 — "reconnect restores state"),
 *   - shard parity: the browser computes the same shard the server fans out to
 *     (`crc32(utf8(ref)) >>> 0) % N`) so it joins the right room (ADR §5.1).
 *
 * Business logic stays server-side: this module only calls
 * `flock_os.engagement_views.*` (FLO-11), which validates window/scope/throttle
 * and re-derives `submitted_at` server-side (source of truth, §6).
 *
 * Exposed on `window.FlockEngageCore` (loaded before the page scripts).
 */
(function () {
	"use strict";

	const WIN = window;
	const NS = "flock_os:event";
	const DB_NAME = "flock_engage_queue";
	const DB_STORE = "pending";
	const DB_VERSION = 1;
	const POLL_MIN_MS = 1500;
	const POLL_MAX_MS = 15000;
	const FLUSH_BATCH = 200;

	// ---- shard parity (ADR §5.1 / FLO-10 §5.1) ----------------------------- //
	// crc32 table (zlib polynomial 0xedb88320) — the JS parity contract for
	// `flock_os.realtime.shard_for` (`crc32(utf8(ref)) % N`). Must be stable
	// across processes/languages; Python's builtin hash() is salted and unusable.
	function makeCrcTable() {
		const table = new Uint32Array(256);
		for (let n = 0; n < 256; n++) {
			let c = n;
			for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
			table[n] = c >>> 0;
		}
		return table;
	}
	const CRC_TABLE = makeCrcTable();

	function crc32(str) {
		const bytes = new TextEncoder().encode(str);
		let crc = 0xffffffff;
		for (let i = 0; i < bytes.length; i++) crc = CRC_TABLE[(crc ^ bytes[i]) & 0xff] ^ (crc >>> 8);
		return (crc ^ 0xffffffff) >>> 0;
	}

	function shardFor(attendeeRef, shardCount) {
		return crc32(attendeeRef) % shardCount;
	}

	function broadcastChannel(sessionId, parity) {
		const p = parity.channels;
		return p.prefix + ":" + sessionId + ":" + p.broadcast_segment;
	}

	function shardChannel(sessionId, shard, parity) {
		const p = parity.channels;
		return p.prefix + ":" + sessionId + ":" + p.shard_segment + ":" + shard;
	}

	// ---- crypto-fair nonce + device fingerprint --------------------------- //
	function uuid() {
		if (WIN.crypto && WIN.crypto.randomUUID) return WIN.crypto.randomUUID();
		return "n-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 10);
	}

	function deviceFingerprint() {
		// A coarse, privacy-respecting hint (UA + screen + tz + session token).
		// Not a tracker — the server hashes it for attendee_key (§5 / §6.2).
		try {
			const nav = WIN.navigator || {};
			const scr = WIN.screen || {};
			const bits = [
				nav.userAgent || "",
				nav.language || "",
				(scr.width || 0) + "x" + (scr.height || 0),
				Intl.DateTimeFormat().resolvedOptions().timeZone || "",
			];
			return crc32(bits.join("|")).toString(16);
		} catch (_e) {
			return "fp-" + Math.random().toString(36).slice(2);
		}
	}

	// ---- IndexedDB offline queue (graceful no-op if unavailable) ---------- //
	function openQueueDb() {
		return new Promise((resolve) => {
			if (!WIN.indexedDB) return resolve(null);
			let req;
			try {
				req = WIN.indexedDB.open(DB_NAME, DB_VERSION);
			} catch (_e) {
				return resolve(null);
			}
			req.onupgradeneeded = () => {
				const db = req.result;
				if (!db.objectStoreNames.contains(DB_STORE)) {
					db.createObjectStore(DB_STORE, { keyPath: "nonce" });
				}
			};
			req.onsuccess = () => resolve(req.result);
			req.onerror = () => resolve(null);
		});
	}

	async function queuePush(db, entry) {
		if (!db) return;
		try {
			await new Promise((resolve, reject) => {
				const tx = db.transaction(DB_STORE, "readwrite");
				tx.objectStore(DB_STORE).put(entry);
				tx.oncomplete = () => resolve();
				tx.onerror = () => reject(tx.error);
			});
		} catch (_e) {
			/* queue is best-effort; the network call still proceeds */
		}
	}

	async function queueAll(db) {
		if (!db) return [];
		return new Promise((resolve) => {
			try {
				const tx = db.transaction(DB_STORE, "readonly");
				const req = tx.objectStore(DB_STORE).getAll();
				req.onsuccess = () => resolve(req.result || []);
				req.onerror = () => resolve([]);
			} catch (_e) {
				resolve([]);
			}
		});
	}

	async function queueDelete(db, nonces) {
		if (!db || !nonces.length) return;
		try {
			await new Promise((resolve) => {
				const tx = db.transaction(DB_STORE, "readwrite");
				const store = tx.objectStore(DB_STORE);
				nonces.forEach((n) => store.delete(n));
				tx.oncomplete = () => resolve();
				tx.onerror = () => resolve();
			});
		} catch (_e) {
			/* best-effort */
		}
	}

	// ---- the hook --------------------------------------------------------- //
	// Returns a controller object the <EngagementStage> host + kind components
	// drive. `state` is the last confirmed server state; `on(type, fn)` subscribes
	// to realtime events; `participate(kind, payload)` records an interaction
	// through the optimistic queue.
	function useEngagementSession(sessionId, opts) {
		const config = opts || {};
		const parity = config.parity;
		const endpoints = config.endpoints || {};
		const i18n = config.i18n || ((s) => s);

		const state = {
			sessionId: sessionId,
			status: null,
			ticket: null,
			attendeeKey: null,
			attendeeRef: null,
			shard: null,
			joined: false,
			participated: false,
			connection: "connecting", // connecting | live | polling | offline
			serverSnapshot: null,
		};

		const listeners = new Map(); // event type -> Set<fn>
		let db = null;
		let pollTimer = null;
		let pollDelay = POLL_MIN_MS;
		let socketLive = false;
		let destroyed = false;

		function emit(type, detail) {
			const fns = listeners.get(type);
			if (fns) fns.forEach((fn) => {
				try { fn(detail); } catch (_e) { /* listener errors are isolated */ }
			});
		}

		function on(type, fn) {
			if (!listeners.has(type)) listeners.set(type, new Set());
			listeners.get(type).add(fn);
			return () => listeners.get(type) && listeners.get(type).delete(fn);
		}

		function setConnection(kind) {
			if (state.connection === kind) return;
			state.connection = kind;
			emit("connection", kind);
		}

		// -- join: get the signed ticket + current server state (§10.2) ------- //
		async function join(extra) {
			const args = Object.assign(
				{ session: sessionId, device_fingerprint: deviceFingerprint() },
				extra || {}
			);
			const res = await call(endpoints.join_session, args);
			if (!res) throw new Error(i18n("Could not join session."));
			applyJoin(res);
			return res;
		}

		function applyJoin(res) {
			state.ticket = res.ticket || null;
			state.attendeeKey = res.attendee_key || null;
			state.attendeeRef = res.attendee_ref || state.attendeeKey || sessionId;
			state.status = res.status || null;
			state.participated = Boolean(res.participated);
			state.serverSnapshot = res.state || null;
			state.joined = true;
			if (parity && state.attendeeRef) {
				state.shard = shardFor(state.attendeeRef, parity.shard_count || 10);
			}
			emit("joined", res);
			if (res.state) emit("state", res.state);
			subscribeRealtime();
		}

		// -- realtime: WS first, polling fallback on drop (§8) ---------------- //
		function subscribeRealtime() {
			const ev = parity ? parity.realtime_events : null;
			const frappeRT = WIN.frappe && WIN.frappe.realtime;
			if (!ev || !frappeRT) {
				startPolling();
				return;
			}
			try {
				const handler = (message) => {
					socketLive = true;
					stopPolling();
					setConnection("live");
					routeRealtime(message);
				};
				[ev.game_state, ev.attendance_count, ev.attendance_presence].forEach((name) => {
					frappeRT.on(name, handler);
				});
				// Connection-quality hooks (Frappe realtime exposes these on socket).
				const sock = frappeRT.socket || (frappeRT.$socket && frappeRT.$socket());
				if (sock) {
					sock.on("connect", () => {
						socketLive = true;
						stopPolling();
						setConnection("live");
						// reconnect/restore: pull the last confirmed server state (§7).
						refreshState();
						flushQueue();
					});
					sock.on("disconnect", () => {
						socketLive = false;
						setConnection("offline");
						startPolling();
					});
				}
				socketLive = Boolean(sock && sock.connected);
				if (socketLive) setConnection("live");
				else startPolling();
			} catch (_e) {
				startPolling();
			}
		}

		function routeRealtime(message) {
			if (!message) return;
			// game_state carries { state: opened|closed, ... } + results on close.
			if (message.state) emit("lifecycle", message);
			if (typeof message.count === "number") emit("headcount", message.count);
			if (message.results) emit("results", message.results);
			if (message.kind || message.feed) emit("feed", message);
		}

		// -- polling fallback with exponential backoff ----------------------- //
		function startPolling() {
			if (pollTimer || destroyed) return;
			setConnection(socketLive ? "live" : state.ticket ? "polling" : "connecting");
			const tick = async () => {
				if (destroyed) return;
				await refreshState();
				pollDelay = Math.min(POLL_MAX_MS, Math.round(pollDelay * 1.7));
				pollTimer = WIN.setTimeout(tick, pollDelay);
			};
			pollTimer = WIN.setTimeout(tick, pollDelay);
		}

		function stopPolling() {
			if (pollTimer) {
				WIN.clearTimeout(pollTimer);
				pollTimer = null;
			}
			pollDelay = POLL_MIN_MS;
		}

		async function refreshState() {
			if (!endpoints.session_state) return;
			const res = await call(endpoints.session_state, { session: sessionId, ticket: state.ticket });
			if (!res) return;
			state.status = res.status || state.status;
			state.serverSnapshot = res.state || state.serverSnapshot;
			state.participated = state.participated || Boolean(res.participated);
			emit("state", res);
			if (typeof res.headcount === "number") emit("headcount", res.headcount);
			if (res.status === "closed") emit("lifecycle", { state: "closed", results: res.results });
		}

		// -- participate: optimistic local queue + network (§6.4 / §8) -------- //
		async function participate(kind, payload) {
			if (!state.joined) throw new Error(i18n("Not joined to a session."));
			const entry = {
				nonce: uuid(),
				session: sessionId,
				kind: kind,
				payload: payload || {},
				ticket: state.ticket,
				client_submitted_at: new Date().toISOString(),
				client_clock: Date.now(),
			};
			await queuePush(db, entry);
			emit("queued", entry);
			// Fire-and-forget single; the queue flush covers the offline gap.
			call(endpoints.participate, entry)
				.then((res) => {
					if (res && res.accepted) {
						state.participated = true;
						emit("participated", { kind: kind, res: res });
					}
					return queueDelete(db, [entry.nonce]);
				})
				.catch(() => {
					/* leaves the entry queued for the next flush */
				});
			return entry.nonce;
		}

		// -- flush the offline queue to the bulk endpoint on reconnect -------- //
		async function flushQueue() {
			if (!endpoints.flush_offline) return;
			const pending = await queueAll(db);
			if (!pending.length) return;
			for (let i = 0; i < pending.length; i += FLUSH_BATCH) {
				const batch = pending.slice(i, i + FLUSH_BATCH);
				const nonces = batch.map((b) => b.nonce);
				try {
					const res = await call(endpoints.flush_offline, { session: sessionId, items: batch });
					if (res && res.accepted_count) {
						state.participated = true;
						emit("flushed", res);
					}
					await queueDelete(db, nonces);
				} catch (_e) {
					/* leave queued; will retry on the next reconnect */
					break;
				}
			}
		}

		// -- low-level frappe.call wrapper ----------------------------------- //
		function call(method, args) {
			return new Promise((resolve) => {
				if (!method || !WIN.frappe || !WIN.frappe.call) {
					resolve(null);
					return;
				}
				WIN.frappe.call({
					method: method,
					args: args,
					callback: (r) => resolve(r && r.message ? r.message : null),
				});
			});
		}

		// -- lifecycle -------------------------------------------------------- //
		async function start(extra) {
			db = await openQueueDb();
			await join(extra);
			// Drain anything left from a previous crashed session.
			await flushQueue();
		}

		function destroy() {
			destroyed = true;
			stopPolling();
			const frappeRT = WIN.frappe && WIN.frappe.realtime;
			const ev = parity ? parity.realtime_events : null;
			if (ev && frappeRT && frappeRT.off) {
				[ev.game_state, ev.attendance_count, ev.attendance_presence].forEach((name) => {
					try { frappeRT.off(name); } catch (_e) { /* noop */ }
				});
			}
			listeners.clear();
		}

		return {
			state: state,
			on: on,
			join: join,
			start: start,
			participate: participate,
			refreshState: refreshState,
			flushQueue: flushQueue,
			destroy: destroy,
			// parity helpers (exposed for the kind components + tests)
			shardFor: function (ref) { return shardFor(ref, parity ? parity.shard_count : 10); },
			broadcastChannel: function () { return broadcastChannel(sessionId, parity); },
			shardChannel: function (shard) { return shardChannel(sessionId, shard, parity); },
		};
	}

	WIN.FlockEngageCore = {
		useEngagementSession: useEngagementSession,
		crc32: crc32,
		shardFor: shardFor,
		broadcastChannel: broadcastChannel,
		shardChannel: shardChannel,
		deviceFingerprint: deviceFingerprint,
		uuid: uuid,
		CONST: { POLL_MIN_MS: POLL_MIN_MS, POLL_MAX_MS: POLL_MAX_MS, FLUSH_BATCH: FLUSH_BATCH, NS: NS },
	};
})();
