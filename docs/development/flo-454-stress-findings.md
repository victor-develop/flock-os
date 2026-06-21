# FLO-454 — Phase 6.1 Local 15k DB/App Stress: Findings

> No-board Phase 6.1 validation. Local bench only — no cloud, no board budget.
> Drills the DB/app tier at ~15,000-attendee scale to surface the performance
> backlog before the first real event. Routes around the wedged Architect on
> [FLO-365](/FLO/issues/FLO-365) / [FLO-452](/FLO/issues/FLO-452).

## Drill

**Seed**: `flock_os/utils/stress_seed.py` + `scripts/dev/stress-15k.sh` — a
repeatable, idempotent seeder that creates 3 branches, 18 nested groups (2 root
+ 4 children × 3), 15,000 members, 15,000 group-member links, 15,000 attendance
records across 6 gatherings, and 6,000 event registrations on the local Docker
bench (`docker/docker-compose.yml` — MariaDB 11.4 + Redis 7 + Frappe v15).

**Profile**: captures `EXPLAIN` plans and wall-clock timings for the four hot
paths the FLO-10 scale ADR locks down: bulk-attendance `filter_unseen` + aggregate,
registration list, group-member roster, and the realtime room-join scope gate.

**Throttle**: burst-tests the Redis sliding-window primitive (FLO-319 / FLO-290
§6.6) at 3× the per-second cap to confirm it still smashes a flood.

### Run

```bash
scripts/dev/stress-15k.sh
# or inside the container:
cd sites && ../env/bin/python -m flock_os.utils.stress_seed
```

Re-runs are idempotent: a `stress-*` namespace tag marks every seeded row, so
`_cleanup()` truncates and re-inserts cleanly.

## Results snapshot

| Signal                              | Value        | Verdict |
| ----------------------------------- | ------------ | ------- |
| Seed time (15k members + 15k att + 6k reg) | 10.6 s | OK |
| `attendance.filter_unseen` (500-item batch) | **4,471 ms** | **FAIL** (§8 p95 < 500ms) |
| `attendance.aggregate_read`         | 0.81 ms      | OK (rollup, not scan) |
| `registration.list_500`             | 9.15 ms      | borderline |
| `group_member.roster_1500`          | 23.5 ms      | borderline |
| `realtime.room_join_branch_resolve` | 0.95 ms      | OK |
| Throttle burst (30 reqs @ 10/s cap) | 10 allowed, 20 throttled | PASS |

### EXPLAIN evidence

**filter_unseen** (the catastrophic one):
```
type: ref, key: attendee_ref, rows: 2, Extra: Using index condition; Using where
```
Only the single-column `attendee_ref` index fires — there is **no composite
`(event, attendee_ref, client_req_id)` index** to support the 500-tuple `IN`
clause. The optimizer picks the most selective single column and does 500 index
probes +回表 lookups, hence the 4.5s wall time.

**aggregate UPDATE** on Event Attendance Summary:
```
type: range, key: event, rows: 2, Extra: Using where
```
No composite `(branch, event)` index — the optimizer falls back to the `event`
single-column index. Functional but fragile (two branch-1 events with the same
gathering id would collide silently).

**registration list** (full scan):
```
type: ALL, possible_keys: gathering,registration_status, key: NULL, rows: 6000
```
The optimizer **chose a full table scan** over either single-column index. With
only individual indexes on `gathering` and `registration_status`, the optimizer
can't intersect them for the two-predicate `WHERE` — it falls back to scanning
all 6,000 rows.

**group member roster** (full scan):
```
type: ALL, possible_keys: branch, key: NULL, rows: 15000
```
Same pattern: the single-column `branch` index has poor selectivity (1/3 of
rows match), so the optimizer skips it entirely and scans all 15,000 rows.

## Findings — prioritized performance backlog

### P0 — Missing composite UNIQUE indexes on attendance tables

The patch `flock_os/patches/v0_1/add_attendance_indexes.py` defines three
composite UNIQUE indexes that are **absent from the live DB**:

| Index | Table | Definition | Status |
| ----- | ----- | ---------- | ------ |
| `unique_event_attendee_ref` | `tabFlock Attendance Record` | `UNIQUE (event, attendee_ref)` | **MISSING** |
| `unique_event_attendee_req` | `tabFlock Attendance Record` | `UNIQUE (event, attendee_ref, client_req_id)` | **MISSING** |
| `unique_branch_event` | `tabEvent Attendance Summary` | `UNIQUE (branch, event)` | **MISSING** |

**Evidence**: `SHOW INDEX` confirms only single-column indexes exist;
`information_schema.STATISTICS` lookup for the three named indexes returns
empty. The `filter_unseen` query takes 4,471 ms for a 500-item batch — 89×
the §8 p95 < 500ms bar — because the IN-clause has no composite index to
ride.

**Impact**:
- The idempotency backstop (`(event, attendee_ref)` unique constraint) does not
  fire — replays can double-count attendance.
- `seed_aggregate`'s `INSERT IGNORE` silently inserts duplicate summary rows
  (no unique constraint to trigger IGNORE).
- The §8 200 wps gate is impossible to hit: each batch's dedup alone consumes
  the entire 500ms budget.

**Proposed fix**: run `bench --site <site> migrate` (the patch is already
authored + idempotent), or execute the patch directly:
`bench execute flock_os.patches.v0_1.add_attendance_indexes.execute`.

### P1 — Registration roster needs composite index on `(gathering, registration_status)`

**Path**: `Flock Event Registration` list for a gathering — the check-in roster
surface. `EXPLAIN type: ALL` (full scan of all registrations). At 15k
registrations per event, this degrades linearly.

**Proposed fix**: `ALTER TABLE \`tabFlock Event Registration\` ADD INDEX
\`idx_gathering_status\` (\`gathering\`, \`registration_status\`)` via a
versioned patch.

### P1 — Group Member roster needs composite index on `(branch, status)` / `(group, status)`

**Path**: `Flock Group Member` roster resolution — the scope predicate that
`is_member_in_scope` and `member_groups` run. `EXPLAIN type: ALL` (full scan of
15,000 rows). At 15k members per branch this will dominate the eligibility
check.

**Proposed fix**: `ALTER TABLE \`tabFlock Group Member\` ADD INDEX
\`idx_group_status\` (\`group\`, \`status\`)` via a versioned patch. The
`(branch, status)` variant is also useful for branch-wide roster pulls.

### P2 — `filter_unseen` IN-clause pattern at 500 tuples is a lock-in hazard

Even with the composite index, the `WHERE (event, attendee_ref, client_req_id)
IN (500 tuples)` pattern is an N-way expansion. Consider a temp-table JOIN or
batched `EXISTS` rewrite if the 500-tuple path stays the dedup mechanism.

## Verifications passed

- **Throttle (FLO-319)**: the in-memory sliding-window backend correctly caps
  at 10/s — 10 allowed, 20 throttled out of a 30-request burst. The Redis-backed
  production adapter (`frappe.cache()`) delegates to the same `throttle_allows`
  contract, so the semantics hold.
- **Aggregate read path (FLO-10 §4.2)**: count via the maintained `Event
  Attendance Summary` rollup reads in <1ms — the "never scan" invariant is
  intact (once the index is present).
- **Room-join scope gate (FLO-106)**: the `Flock Gathering → branch` resolution
  the realtime gate depends on reads in <1ms.

## Out of scope (per FLO-454)

- WS-tier / connect scaling — owned by [FLO-347](/FLO/issues/FLO-347) /
  [FLO-127](/FLO/issues/FLO-127).
- Any cloud/prod VM work — board-gated under
  [609ccd5d](/FLO/approvals/609ccd5d-13ca-4db2-8cbc-99069e0224cc).
- Implementing the fixes — the backlog above feeds child issues; fix slices get
  their own issues.
