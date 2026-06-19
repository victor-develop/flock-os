// Flock OS – realtime shard + channel helpers (FLO-49 / FLO-10 §5.1).
//
// Pure-JS port of flock_os.realtime so the k6 ws client computes the SAME
// shard / channel as the Python projector. This is the JS parity contract
// documented in flock_os/realtime.py (shard_for docstring):
//
//     shard_for(ref) == (crc32(utf8(ref)) >>> 0) % N
//
// The Python golden values are pinned by flock_os/tests/test_shard_parity.py;
// `load/lib/shards.test.mjs` mirrors a subset so `node --test` keeps the two
// languages in lockstep without a running bench.

export const EVENT_ROOM_PREFIX = "flock_os:event";
export const BROADCAST_SEGMENT = "broadcast";
export const SHARD_SEGMENT = "shard";
export const DEFAULT_SHARD_COUNT = 10;

// IEEE CRC-32 table (same polynomial as Python zlib.crc32 / gzip).
const CRC_TABLE = (() => {
	const table = new Uint32Array(256);
	for (let n = 0; n < 256; n++) {
		let c = n;
		for (let k = 0; k < 8; k++) {
			c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
		}
		table[n] = c >>> 0;
	}
	return table;
})();

// utf-8 encode. Node exposes `TextEncoder` globally, but k6's (v2) pure-JS
// runtime does NOT — `new TextEncoder()` throws ReferenceError there and aborts
// every ws_event_room.js iteration before ws.connect (FLO-104). Feature-detect
// and fall back to a manual encoder that stays byte-identical to Python's
// `str.encode("utf-8")` (golden-pinned by shards.test.mjs / test_shard_parity.py).
const _textEncoder = typeof TextEncoder !== "undefined" ? new TextEncoder() : null;

// Manual UTF-8 encoder (no globals). Exported so shards.test.mjs can pin it
// byte-for-byte against TextEncoder even under Node, guarding the k6 path that
// node tests otherwise can't reach.
export function utf8Fallback(str) {
	const out = [];
	for (let i = 0; i < str.length; i++) {
		let c = str.charCodeAt(i);
		if (c < 0x80) {
			out.push(c);
		} else if (c < 0x800) {
			out.push(0xc0 | (c >> 6), 0x80 | (c & 0x3f));
		} else if (c >= 0xd800 && c <= 0xdbff && i + 1 < str.length) {
			// surrogate pair -> U+10000..U+10FFFF
			const c2 = str.charCodeAt(++i);
			const cp = 0x10000 + ((c & 0x3ff) << 10) + (c2 & 0x3ff);
			out.push(
				0xf0 | (cp >> 18),
				0x80 | ((cp >> 12) & 0x3f),
				0x80 | ((cp >> 6) & 0x3f),
				0x80 | (cp & 0x3f),
			);
		} else {
			out.push(0xe0 | (c >> 12), 0x80 | ((c >> 6) & 0x3f), 0x80 | (c & 0x3f));
		}
	}
	return out;
}

function utf8(str) {
	return _textEncoder ? _textEncoder.encode(str) : utf8Fallback(str);
}

// Unsigned CRC-32 of a string, identical to Python's `zlib.crc32(s.encode()) >>> 0`.
export function crc32(str) {
	const bytes = utf8(str);
	let crc = 0xffffffff;
	for (let i = 0; i < bytes.length; i++) {
		crc = CRC_TABLE[(crc ^ bytes[i]) & 0xff] ^ (crc >>> 8);
	}
	return (crc ^ 0xffffffff) >>> 0;
}

// Stable shard for an attendee: crc32(ref) % shardCount (FLO-10 §5.1).
export function shardFor(attendeeRef, shardCount = DEFAULT_SHARD_COUNT) {
	return crc32(attendeeRef) % shardCount;
}

// The presence/room shard room an attendee joins.
export function shardChannel(eventId, shard) {
	return `${EVENT_ROOM_PREFIX}:${eventId}:${SHARD_SEGMENT}:${shard}`;
}

// The shared broadcast room every event-room client also joins (admin pushes).
export function broadcastChannel(eventId) {
	return `${EVENT_ROOM_PREFIX}:${eventId}:${BROADCAST_SEGMENT}`;
}

// Both rooms one client must subscribe to (its shard + the broadcast).
export function roomsFor(eventId, attendeeRef, shardCount = DEFAULT_SHARD_COUNT) {
	return [shardChannel(eventId, shardFor(attendeeRef, shardCount)), broadcastChannel(eventId)];
}
