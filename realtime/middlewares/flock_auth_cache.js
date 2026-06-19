// flock_os realtime per-connection auth cache (FLO-116 / FLO-14 / FLO-10 §8).
//
// Problem: Frappe's site-namespace auth middleware (vendored
// apps/frappe/realtime/middlewares/authenticate.js) fires ONE synchronous HTTP
// callback per connection — GET /api/method/frappe.realtime.get_user_info — to
// resolve socket.user. At the §8 15k bar the k6 smoke logs in once (setup())
// and every VU presents the SAME `sid`, so the node realtime server makes ~15k
// identical get_user_info round-trips through gunicorn. Under that burst the
// superagent calls hit ETIMEDOUT (errno -60), so connections cycle/fail
// (connect p95 2.26 s), in-flight packets drop (flock_ws_receive_errors 8255),
// and broadcasts back up (p95 16.41 s). Redis is NOT the wall. Root cause =
// the per-connection auth HTTP, not the broadcast fan-out.
//
// Fix: cache the resolved identity by `sid` (the cookie already on the socket)
// so get_user_info fires ONCE per session, not once per connection. At 15k
// clients sharing one sid that is 1 call + 14,999 in-memory hits — clearing the
// §8 wall. Vendored Frappe stays untouched: this module wraps the original
// `authenticate` middleware and is wired in by
// scripts/dev/wire-socketio-auth-cache.sh using the same marker-guarded,
// idempotent pattern as the room-join handler (wire-socketio-handler.sh).
//
// Security tradeoff (deliberate, bounded): on a cache HIT the wrapper replays
// the cached {user,user_type} and skips the redundant get_user_info HTTP. The
// sid already passed frappe's FULL validation (namespace + origin + cookie +
// get_user_info) when it was first cached, and entries expire on a short TTL,
// so a revoked/changed session is re-checked within the TTL window. This is the
// issue's "short-circuits on a known-good sid" option and avoids duplicating
// frappe's validation logic (DRY). TTL + LRU bound staleness and memory;
// Redis-push logout invalidation is an optional follow-up (see
// docs/development/ws-broadcast-delivery.md -> auth cache).
//
// `wrap(originalAuth, options)` returns a Socket.IO namespace middleware with
// the same `(socket, next)` signature frappe's index.js registers. `originalAuth`
// is injected (the already-required `authenticate` const) so this module stays
// decoupled from frappe's path and unit-testable with no bench — the same
// injection style the room-join handler uses for `frappe_request`.
"use strict";

// Defaults. Tunable via `options` (the wiring passes none -> these apply). The
// TTL is conservative: a revoked session is re-checked within this window. The
// §8 burst is ~120 s, so a 60 s TTL yields 1 call + 14,999 hits per window —
// well under the wall. MAX bounds memory across distinct sessions (LRU).
const DEFAULT_TTL_MS = 60_000;
const DEFAULT_MAX = 50_000;

// Environment overrides (node realtime process). Parsed at wrap() time so a
// config change is a process restart, not a per-connection branch.
function _envInt(name, fallback) {
	const raw = process.env[name];
	if (!raw) return fallback;
	const n = Number.parseInt(raw, 10);
	return Number.isFinite(n) && n > 0 ? n : fallback;
}

// Bounded, TTL'd, access-promoted (LRU) cache keyed by sid (or Authorization).
// Map preserves insertion order; delete+set on a hit promotes the entry to the
// end (most-recent), so evicting the first key is true LRU.
class AuthCache {
	constructor({ ttlMs = DEFAULT_TTL_MS, max = DEFAULT_MAX } = {}) {
		this.ttlMs = ttlMs;
		this.max = max;
		// key -> { user, user_type, sid, authorization_header, exp }
		this._entries = new Map();
		this.stats = { hits: 0, misses: 0, evictions: 0, stores: 0 };
	}

	get(key) {
		const entry = this._entries.get(key);
		if (!entry) return undefined;
		if (entry.exp <= Date.now()) {
			// expired -> drop + treat as a miss
			this._entries.delete(key);
			return undefined;
		}
		// LRU promote: re-insert at the tail.
		this._entries.delete(key);
		this._entries.set(key, entry);
		this.stats.hits++;
		return entry;
	}

	set(key, value) {
		if (this._entries.has(key)) this._entries.delete(key);
		while (this._entries.size >= this.max) {
			const oldest = this._entries.keys().next().value;
			if (oldest === undefined) break; // max <= 0 guard
			this._entries.delete(oldest);
			this.stats.evictions++;
		}
		this._entries.set(key, { ...value, exp: Date.now() + this.ttlMs });
		this.stats.stores++;
	}

	clear() {
		this._entries.clear();
	}

	get size() {
		return this._entries.size;
	}
}

// Extract the cache key from the incoming handshake BEFORE delegating. Mirrors
// what frappe's authenticate reads (cookie `sid` first, else Authorization).
// Frappe sids are opaque session tokens (never URL-encoded), so a hand-rolled
// regex matches `cookie.parse` for the `sid` key without pulling in frappe's
// node-only `cookie` dep — keeping this module dependency-free under
// `node --test`. If the keys ever diverge the worst case is a no-op cache miss
// (correctness preserved; just no caching benefit), never an auth bypass.
function cacheKey(socket) {
	const headers = (socket && socket.request && socket.request.headers) || {};
	const sidMatch = /(?:^|;\s*)sid=([^;]+)/.exec(headers.cookie || "");
	if (sidMatch) return sidMatch[1];
	return headers.authorization || null;
}

// Build the cached Socket.IO middleware around frappe's original authenticate.
// `options.ttlMs` / `options.max` override the env-derived defaults (tests).
function wrap(originalAuth, options = {}) {
	const cache = new AuthCache({
		ttlMs: options.ttlMs ?? _envInt("FLOCK_AUTH_CACHE_TTL_MS", DEFAULT_TTL_MS),
		max: options.max ?? _envInt("FLOCK_AUTH_CACHE_MAX", DEFAULT_MAX),
	});

	function authenticate_cached(socket, next) {
		const key = cacheKey(socket);
		const entry = key ? cache.get(key) : undefined;

		if (entry) {
			// HIT: replay the resolved identity frappe's authenticate would have
			// set after a successful get_user_info. Skips the redundant HTTP.
			socket.user = entry.user;
			socket.user_type = entry.user_type;
			socket.sid = entry.sid;
			socket.authorization_header = entry.authorization_header;
			return next();
		}

		cache.stats.misses++;
		// MISS: run frappe's FULL authenticate (all its validation + the
		// get_user_info HTTP), then capture the resolved identity on success so
		// the next connection with the same sid hits.
		originalAuth(socket, (err) => {
			if (err) return next(err);
			const storeKey = socket.sid || socket.authorization_header;
			if (storeKey && socket.user) {
				cache.set(storeKey, {
					user: socket.user,
					user_type: socket.user_type,
					sid: socket.sid,
					authorization_header: socket.authorization_header,
				});
			}
			next();
		});
	}

	// Exposed for tests + ops introspection (not used by the wire path).
	authenticate_cached.cache = cache;
	return authenticate_cached;
}

module.exports = {
	wrap,
	AuthCache,
	cacheKey,
	DEFAULT_TTL_MS,
	DEFAULT_MAX,
};
