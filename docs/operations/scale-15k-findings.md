# 15k-Scale DB/App Performance Stress — Findings (FLO-365)

> Data-tier stress drill at ~15,000-attendee event volume on the local bench.
> Distinct from the WS-tier work ([FLO-347](/FLO/issues/FLO-347) /
> [FLO-121](/FLO/issues/FLO-121)): this exercises the **MariaDB data paths**
> (attendance, registration, realtime scope resolution) at event volume.

## Method

1. **Seed** a realistic dataset via `flock_os.utils.scale_seed` (idempotent;
   purgeable via the `scale-*` marker): 1 org, 5 branches, 15 nested groups,
   **15,000 members**, 5 anchor gatherings + 1 one-time event, **15,000
   attendance rows** inserted **through the canonical `BulkAttendanceService`**
   (the real production write path), and 15,000 registrations.
2. **Profile** via `flock_os.utils.scale_profile`: best-of-5 timings (2 warmup),
   `EXPLAIN` query plans, N+1 / full-scan flags, and an in-app throttle burst
   check.
3. **Repeatable**: re-run `scripts/dev/seed-15k-scale.sh` +
   `scripts/dev/profile-15k-scale.sh` on a clean bench; the seeder purges tagged
   rows first so the drill is deterministic.

Environment: local bench (Frappe 15.112.0, MariaDB 12.3, Redis, macOS). The bench
was migrated to current schema first (`bench migrate` applied the
v0_1/v0_2/v0_3 index patches). Raw report: `scale-15k-profile-report.json`.

## Seed throughput

| Path | Volume | Time | Throughput |
| --- | --- | --- | --- |
| Bulk attendance (canonical `BulkAttendanceService`) | 15,000 rows (30×500 batches) | ~3s | **~5,000 rows/s** |
| Members (bulk SQL) | 15,000 | ~3s | — |
| Full seed (structural + attendance + registrations) | 45,000+ rows | **9.4s** | — |

The 15k bulk-attendance drain lands well inside the §8 60s budget.

## Hot-path timings (median ms, best-of-5, over the 15k dataset)

| Hot path | Median (ms) | Verdict |
| --- | --- | --- |
| `attendance.aggregate` (rollup read) | **0.73** | ✅ Maintained `Event Attendance Summary` — O(1). |
| `attendance.aggregate` (`COUNT(*)` anti-pattern) | **4.06** | ⚠️ 5.6× slower than the rollup at 3k rows/branch; widens fast. |
| `attendance.bulk_write` (500-row batch) | **97** (5,137 rows/s) | ✅ Healthy. |
| `registration.dashboard` (counter + waitlist COUNT) | **2.55** | ✅ Counter-only; waitlist COUNT indexed. |
| `registration.gateway_gathering_reads` (branch+group+org) | **1.37** (3 queries) | ⚠️ N+1 — 3 SELECTs collapsible to 1. |
| `realtime.room_join_scope` (gathering→branch + perms) | **1.20** | ✅ Per-join cost is low. |
| `attendance.scoped_list` (branch, limit 100) | **16.4** | 🔴 Slowest by far — index choice issue (below). |

## Throttle verification (§6.6 / FLO-319)

The per-device 1s sliding-window throttle (`flock_os.rate_limit`) **holds under
burst**: 10 calls allowed (the cap), 5 rejected — `holds=true`. The in-memory
primitive was exercised; the production Redis backend shares the identical
`throttle_allows` contract (FLO-319), so the rule is verified.

## Prioritized performance backlog

Each item is filed as a child issue with the evidence + proposed fix. Ordered by
impact × severity.

### HIGH

- **PERF-CHK-NONATOMIC** — `check_in_registration._bump` does read-then-write
  (`get_value` + `set_value`) for `checked_in_count`: **non-atomic** (loses
  updates under concurrent check-in) AND costs 2 queries where 1 suffices. The
  sibling `_bump_registered_count` already does the correct atomic
  `UPDATE … = count + 1`. Mirror it. *(Correctness + perf.)*

- **PERF-BULK-FORUPDATE** — `process_bulk_batch` calls
  `_authoritative_registration_status` (`SELECT … FOR UPDATE` on the gathering
  row) **per member** → 500 serial row locks per 500-row batch. This serializes
  the 15k bulk-registration ingest. Decide capacity once per batch (or
  batch-chunk) under a single lock, not per member.

### MEDIUM

- **PERF-LIST-IDX** — the branch-scoped attendance list view (the hottest scoped
  read) picks the **`modified` index** for `ORDER BY modified DESC LIMIT 100`,
  scanning ~1,427 rows with "Using where" instead of riding the `branch` index
  → **16ms median** at 3k rows/branch. Add a composite `(branch, modified DESC)`
  index so the filter + sort share one index (or defer the sort).

- **PERF-REG-N1** — `FrappeRegistrationScopeGateway.gathering_branch/group/
  organization` are 3 separate `get_value` calls; `register_for_event` and
  `process_bulk_batch` invoke all three. Collapse to a single
  `db.get_value(gathering, ['branch','group','organization'], as_dict=True)`.

- **PERF-INVITE-N1** — `has_valid_invitation` loops `invitee_groups` calling
  `group_subtree` (a recursive tree query) **plus** a per-group
  `get_value(expires_on)`: `O(invitations × subtree depth)` at eligibility-check
  time. Bulk-resolve subtrees once; fold the expiry check into the single
  invitation fetch.

### LOW

- **PERF-AGG-SUM** — `FrappeBulkAttendanceGateway.aggregate` (no event) fetches
  every `Event Attendance Summary` row for a branch then Python `sum()`s them —
  grows with event count per branch. Replace with `SELECT SUM(total) … WHERE
  branch=%s` at the DB.

## What is already correct (no action)

- The **maintained-aggregate read** (rollup) is the sanctioned count path and is
  sub-ms — the FLO-10 §4.2 invariant holds at 15k.
- The **bulk-write path** (READ COMMITTED + tight tx + idempotency) sustains
  ~5k rows/s on the local bench — comfortably inside the §8 budget.
- **Throttle** (FLO-319) holds under burst.
- **WS room-join scope** resolution is per-join-cheap (the WS-tier ceiling is
  connection-count, owned by FLO-347, not this data-tier concern).

## Out of scope

- WS-tier / connect scaling — [FLO-347](/FLO/issues/FLO-347) / [FLO-127](/FLO/issues/FLO-127).
- Implementing the fixes — this issue produces the backlog; fix slices get their
  own child issues.
- Any cloud/prod VM work — board-gated under
  [609ccd5d](/FLO/approvals/609ccd5d-13ca-4db2-8cbc-99069e0224cc).

## Repeat the drill

```bash
# From the bench dir (BENCH_DIR=/Users/mac/opencode-workspace-default/flock-os-bench)
scripts/dev/seed-15k-scale.sh flock_os.localhost     # idempotent; purges first
scripts/dev/profile-15k-scale.sh flock_os.localhost  # logs the JSON report
```
