# Flock OS — Scale load harness (`load/`)

DevOps-owned runtime half of the FLO-10 §8 verification bar (issue
[FLO-49](https://example.invalid/FLO/issues/FLO-49), carved from
[FLO-16](https://example.invalid/FLO/issues/FLO-16)). QA owns the 15k-fixture
**test** gate (`flock_os/tests/test_scale_attendance.py`, green in CI); this
directory owns the **runtime load + telemetry** gate that the Phase 6 release
is gated on.

## Acceptance bar (FLO-10 §8)

| Signal                              | Target                                  | Measured by                 |
| ----------------------------------- | --------------------------------------- | --------------------------- |
| Bulk write throughput               | 200 writes/sec × 150s (≈30k, 2× 15k)    | `bulk_attendance.js`        |
| Bulk receipt p95                    | **< 500 ms**                            | `bulk_attendance.js` + telemetry histogram |
| Failed bulk requests                | **0**                                   | `bulk_attendance.js`        |
| Double-counts on replay             | **0** (per-item `client_req_id` dedupe) | QA 15k fixture test         |
| WS connect p95 / broadcast latency  | **< 1 s** at 15k concurrent             | `ws_event_room.js`          |
| Queue drain post-burst              | **< 60 s**                              | telemetry `redis.rq_depth`  |
| MariaDB slow queries                | **0**                                   | telemetry `mariadb.slow_query_count` |

## Prerequisites

- **k6** ≥ 0.50 (`brew install k6`). The smoke scripts are pure k6 (no xk6 extensions).
- **A reachable Frappe runtime**: bench site with MariaDB + Redis, the `flock_os`
  app installed + migrated, the bulk endpoint served (`/api/method/
  flock_os.attendance.bulk_submit`), and the Socket.IO server up. See
  **Runtime fixtures** below — the smoke is inert without this.
- **Node** ≥ 20 only for the shard-parity check (`node --test load/lib/*.test.mjs`),
  which runs with **no runtime** and keeps the JS shard math byte-identical to
  Python (`flock_os/realtime.py`).

## Runtime fixtures (must be seeded before a full run)

The smoke authenticates as a Frappe user whose **single `Flock Branch` User
Permission** resolves the batch scope (`flock_os.attendance._resolve_caller_branch_scope`).

1. A **Flock Branch** (e.g. `branch-smoke`) + a **Flock Gathering** (`gathering-smoke`).
2. A Frappe user `leader@flock.os` / `flock` with the **`Flock Branch` User
   Permission** = `branch-smoke` (single, so the scope is unambiguous).
3. MariaDB tuned + the `(event, attendee_ref)` unique index + `Event Attendance
   Summary` aggregate from FLO-17 migrated.
4. Redis (cache + queue + socketio) + RQ worker on the `flock_attendance` queue.

Override any of these via env (`EVENT_ID`, `BRANCH_ID`, `FLOCK_USER`,
`FLOCK_PASSWORD`, `BASE_URL`, `WS_BASE_URL`) — see `config.js`.

## Scaled-down local smoke (no 30k run)

```bash
# Writes — 20 writes/sec × 30s against the local bench.
k6 run -e WRITES_PER_SEC=20 -e DURATION_SEC=30 -e BATCH_ITEMS=10 \
      -e FLOCK_USER=leader@flock.os -e FLOCK_PASSWORD=flock \
      bulk_attendance.js

# Websocket — 200 concurrent clients × 30s.
k6 run -e WS_VUS=200 -e WS_DURATION_SEC=30 ws_event_room.js
```

## Full acceptance bar (Phase 6 gate)

```bash
# 1. Bulk writes: ramp to 200 writes/sec × 150s (≈30k records).
k6 run -e WRITES_PER_SEC=200 -e DURATION_SEC=150 bulk_attendance.js

# 2. While the write burst runs, drive a broadcast producer (see WS below) and:
k6 run -e WS_VUS=15000 -e WS_DURATION_SEC=120 ws_event_room.js
```

The k6 `thresholds` in each script encode the §8 targets — a non-zero exit
means the gate **failed**. In CI this runs from
`.github/workflows/k6-smoke.yml` against `$BASE_URL` (Phase 6 tag / manual).

## WS broadcast producer

`ws_event_room.js` measures `flock_ws_broadcast_latency` from the `ts` field in
a published realtime message back to receipt. Drive the producer through Frappe
while clients are connected so the latency budget is exercised:

```python
# From a bench console, publish a broadcast-count tick with a timestamp:
frappe.publish_realtime(
    "flock_os:attendance:count",
    message={"delta": 1, "ts": int(time.time() * 1000)},
    room="flock_os:event:gathering-smoke:broadcast",
)
```

## Telemetry (D3/D5 revisit triggers)

`flock_os/telemetry.py` is the dashboard/scrape surface. `TelemetryCollector`
holds the bulk-latency histogram and the D3 (Redis) + D5 (MariaDB) source ports.
Scrape one snapshot:

```python
from flock_os.telemetry import snapshot
print(snapshot().as_dict())
# {
#   "redis":  {"pubsub_messages_per_sec": ..., "connected_clients": ..., "rq_depth": ...},
#   "mariadb": {"slow_query_count": ..., "connections": ..., "buffer_pool_hit_ratio": ...},
#   "bulk_latency": {"count": ..., "p95_seconds": ..., "buckets": [...], "bucket_counts": [...]},
# }
```

- **D3 trigger** (Redis cluster escape hatch): watch `redis.pubsub_messages_per_sec`
  + `connected_clients` + `rq_depth`. A non-draining `rq_depth` > 60s after the
  burst is the queue-budget revisit signal.
- **D5 trigger** (MariaDB scale): watch `mariadb.slow_query_count` rate +
  `buffer_pool_hit_ratio`. A falling hit ratio / rising slow queries triggers
  index/tune/scale.
- **§8 gate**: `bulk_latency.p95_seconds < 0.5`.

The production Frappe source adapters (`FrappeRedisMetricsSource`,
`FrappeMariaDBMetricsSource`) are wired by `flock_os.telemetry.install_frappe_sources()`
from `hooks.py` once a bench is running; they are defensive (a scrape never
breaks the runtime) and import-clean without a bench.

## Runtime status

The k6 + telemetry **artifacts** here are complete and green in CI
(ruff + pytest, incl. the shard-parity cross-language lock). **Executing** the
full 15k acceptance run + live dashboards requires a deployable Frappe bench
site, which is the runtime blocker tracked on FLO-49.
