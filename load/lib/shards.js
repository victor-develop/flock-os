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

// utf-8 encode (TextEncoder is available in both Node and k6).
function utf8(str) {
	return new TextEncoder().encode(str);
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
