// Flock OS – bulk-attendance write smoke (FLO-49 / FLO-10 ADR §8).
//
// Ramps the bulk-reporting write path to 200 writes/sec x 150s (~30k attendance
// records, 2x the 15k worst case) against the FLO-15 whitelisted endpoint
//   POST /api/method/flock_os.attendance.bulk_submit
// and gates FLO-10 §8 acceptance: p95 < 500ms, 0 failed requests.
//
// Each iteration submits one bulk batch of BATCH_ITEMS records; the arrival
// rate is derived so total records/sec == WRITES_PER_SEC regardless of batch
// size (rate = WRITES_PER_SEC / BATCH_ITEMS). Records carry a per-item
// client_req_id (idempotency key) so replays never double-count (FLO-10 §4.1).
//
//   Scaled-down local smoke (no full 30k run):
//     k6 run -e WRITES_PER_SEC=20 -e DURATION_SEC=30 -e BATCH_ITEMS=10 \
//           -e FLOCK_USER=leader@flock.os -e FLOCK_PASSWORD=flock bulk_attendance.js
//
//   Full acceptance bar (Phase 6 gate):
//     k6 run -e WRITES_PER_SEC=200 -e DURATION_SEC=150 bulk_attendance.js
import http from "k6/http";
import { check, sleep } from "k6";
import { Counter } from "k6/metrics";

import { write, bulkUrl } from "./config.js";
import { login } from "./lib/auth.js";

// Custom metric: total attendance records accepted by the queue (per receipt).
const recordsQueued = new Counter("flock_records_queued");

// Derived arrival rate so records/sec == WRITES_PER_SEC at any BATCH_ITEMS.
const ratePerSec = Math.max(1, Math.round(write.writesPerSec / write.batchItems));
const holdSec = Math.max(10, write.durationSec - write.rampUpSec);

export const options = {
	// FLO-10 §8 acceptance thresholds.
	thresholds: {
		http_req_duration: [`p(95)<${write.p95Millis}`],
		http_req_failed: ["rate==0"],
		checks: ["rate==1"],
	},
	scenarios: {
		bulk_writes: {
			executor: "ramping-arrival-rate",
			startRate: 0,
			timeUnit: "1s",
			preAllocatedVUs: Math.max(ratePerSec * 4, 20),
			maxVUs: Math.max(ratePerSec * 20, 100),
			stages: [
				{ target: ratePerSec, duration: `${write.rampUpSec}s` },
				{ target: ratePerSec, duration: `${holdSec}s` },
			],
		},
	},
};

// Per-VU session jar, lazily created on the first iteration so the init context
// (and `k6 inspect`) doesn't fire login I/O. Each VU resolves its own branch
// scope from the caller's User Permission once, then reuses the session.
let sessionJar = null;

export default function () {
	if (sessionJar === null) {
		sessionJar = login(write);
	}
	// Globally-unique attendee refs across VUs/iterations -> no accidental
	// cross-dedupe; the (event, attendee_ref) unique index stays the backstop.
	const batchId = `batch-${__VU}-${__ITER}`;
	const items = [];
	for (let i = 0; i < write.batchItems; i++) {
		const attendeeRef = `${write.branchId}-member-${__VU}-${__ITER}-${i}`;
		items.push({
			attendee_ref: attendeeRef,
			client_req_id: `${batchId}:${i}`,
			status: "Present",
			source: "k6_smoke",
		});
	}

	const payload = JSON.stringify({
		event: write.eventId,
		batch_id: batchId,
		items,
	});

	const res = http.post(bulkUrl(), payload, {
		jar: sessionJar,
		headers: { "Content-Type": "application/json" },
	});

	const ok = check(res, {
		"bulk_submit returns 200": (r) => r.status === 200,
		"receipt accepted": (r) => {
			try {
				// Frappe wraps a whitelisted dict return in {"message": {...}}.
				const raw = r.json();
				const body = (raw && raw.message) || raw;
				return body && body.accepted === true && body.queued === items.length;
			} catch {
				return false;
			}
		},
	});

	if (ok) {
		recordsQueued.add(items.length);
	}
}
