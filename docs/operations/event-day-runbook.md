# Event-day runbook — Flock OS (FLO-581 Phase 6.2)

> The playbook for running one real **~15,000-attendee event** end-to-end on
> Flock OS — the most strategically valuable ops artifact for Phase 6
> ([FLO-231](/FLO/issues/FLO-231)). It covers the full operational lifecycle:
> the **T-7 day pre-event checklist**, **T-0 event-day monitoring**, **live
> attendance ops** (engagement games, attendance capture, scoped
> notifications), **incident escalation**, and **post-event retrospective**.
>
> **Endorsement-independent (like [FLO-544](/FLO/issues/FLO-544)).** This is
> pure design/doc work: it commits no budget, needs no prod VM, and does **not**
> presuppose the board's Phase 6 endorsement
> ([609ccd5d](/FLO/approvals/609ccd5d-13ca-4db2-8cbc-99069e0224cc)). The
> operational logic is the same regardless of which cloud the board picks.
> Every command that depends on infra that does not exist yet is flagged
> **🚧 Phase 6.1 landing needed** rather than fabricated.
>
> **This is an index, not a re-statement.** It links to the specialised
> runbooks (deploy/rollback, backup/restore, migration, MVP triage, realtime)
> instead of duplicating them. If you can read only one section, read
> [Incident escalation](#4-incident-escalation).

## TL;DR

The event-day shape is three nested windows:

```
T-7d  pre-event checklist  ──┐
                             │  capacity, DNS/TLS, backup baseline,
T-1d  go/no-go re-walk       │  secrets, comms tree, go/no-go gate
                             │
T-0   doors open ─────────── ┤  watch dashboards, run engagement,
      event live             │  confirm attendance writes, fan out
                             │  scoped notifications, triage by severity
                             │
T+1   doors close ───────────┘  stop sessions, post-event backup,
      retrospective             reconcile cost, write the retro
```

The single source of truth for "a meeting/event happened" is a **`Flock
Gathering`** row (anchored on `branch` + `group`). Every event-day surface —
realtime rooms, attendance, registration, engagement, scoped notifications —
keys off one gathering id. Know that id before doors open.

## 0. Prerequisites — what must already be true

> These are owned by the Phase 6.1 / 6.2 pipeline and walked at the **launch
> go/no-go** ([`launch-go-no-go.md`](launch-go-no-go.md)). They are
> pre-requisites for event-day, not part of the T-7 checklist. A no-go on any
> of these means **do not run a real event** — fall back to a simulated event
> per the Phase 6.4 plan.

- [ ] Production is live and the launch go/no-go was a unanimous **GO**
      ([`launch-go-no-go.md`](launch-go-no-go.md) sign-off block signed by
      DevOps + QA + CEO + Architect).
- [ ] The §8 scale targets are **reproduced at 15k** on the prod-shape tier
      (signals S4–S7: WS connect p95 < 1s, broadcast p95 < 1s,
      `flock_ws_receive_errors == 0`, 100% sessions) — see
      [`ws-broadcast-delivery.md` → §8 gate](../development/ws-broadcast-delivery.md#running-the-clean-full-15k-8-gate).
- [ ] The scaled-socketio tier is **N processes** post-migrate (not collapsed
      to 1) and a **dedicated adapter Redis** is wired
      ([`migration-runbook.md` §6](migration-runbook.md#6-the-realtime-tier-across-migrations)).
- [ ] The restore drill is green against a real backup
      ([`backup-restore.md`](backup-restore.md)) and **edge rate-limiting** is
      active on registration + realtime-connect (signals S12–S14).

> 🚧 **Phase 6.1 landing needed.** Every prod command below assumes the staging
> VM ([FLO-249](/FLO/issues/FLO-249)), deploy pipeline
> ([FLO-246](/FLO/issues/FLO-246)), and edge rate-limit
> ([FLO-294](/FLO/issues/FLO-294)) are live. Until the board endorses
> ([609ccd5d](/FLO/approvals/609ccd5d-13ca-4db2-8cbc-99069e0224cc)), treat the
> T-7/T-0 command blocks as **the operational contract the pipeline must
> satisfy**, not executable runbook lines.

> 📎 **Cross-linked sibling runbooks land on their own slices** —
> [`scale-15k-findings.md`](scale-15k-findings.md)
> ([FLO-365](/FLO/issues/FLO-365)), [`launch-go-no-go.md`](launch-go-no-go.md)
> ([FLO-332](/FLO/issues/FLO-332)/[FLO-357](/FLO/issues/FLO-357)), and
> [`migration-runbook.md`](migration-runbook.md)
> ([FLO-332](/FLO/issues/FLO-332)/[FLO-350](/FLO/issues/FLO-350)) are real and
> correctly shaped but **not yet on `master`**; the links resolve once those
> Phase 6.1/6.2 slices merge. They dangle only if this slice merges first (see
> [Out of scope](#out-of-scope)).

---

## 1. T-7 days — pre-event checklist

Walk this **seven days before** the event. Each item has an owner, a done-criteria,
and a go/no-go signal. A red on any item at T-1d is a no-go.

### 1a. Capacity check (concurrency budget)

Size the realtime + data tier against the **expected** concurrency, not the
headline attendee count. A 15k-headline event rarely has 15k *simultaneous*
sockets, but plan for the peak room.

| Resource | How to check | Target at 15k peak |
|----------|--------------|--------------------|
| socketio worker count | `supervisorctl status socketio-tier` → **N** processes (not 1) | N ≥ 4 behind the nginx sticky-L7 LB ([`ws-broadcast-delivery.md` → Scaling the tier](../development/ws-broadcast-delivery.md#scale-the-socketio-tier-flo-121)) |
| Dedicated adapter Redis | `scripts/dev/start-adapter-redis.sh status` (or the prod equivalent) | `PING` ok; `FLOCK_SIO_ADAPTER_REDIS` set on every backend |
| Redis (cache + queue) headroom | `redis-cli info clients` / `info stats` | `connected_clients` < 80% of `maxclients`; pub/sub not saturated |
| MariaDB connection headroom | `SHOW STATUS LIKE 'Threads_connected';` vs `max_connections` | < 70% utilised; buffer-pool hit ratio > 99% |
| Bulk-attendance budget | re-run the FLO-365 profile if the gathering shape changed | bulk-write ≥ ~5,000 rows/s inside the 60s budget ([`scale-15k-findings.md`](scale-15k-findings.md)) |

```bash
# 🚧 Phase 6.1 landing needed — these run against prod, not the dev bench.
# On the prod VM (or staging carrying the same shape), as the bench user:
docker exec flock-os supervisorctl status socketio-tier   # expect N, not 1
docker exec flock-os redis-cli -u "$REDIS_SOCKETIO_URI" ping
docker exec flock-os redis-cli -u "$REDIS_CACHE_URI" info clients | grep connected_clients
docker exec flock-os mariadb -u root -p"$DB_ROOT_PASSWORD" \
  -e "SHOW STATUS LIKE 'Threads_connected'; SHOW VARIABLES LIKE 'max_connections';"
```

- [ ] socketio-tier shows N workers; adapter Redis reachable from every backend.
- [ ] Redis + MariaDB have ≥30% headroom at the expected peak.

> If capacity is short, **scale the tier before T-0**, not during — restarting
> `socketio-tier` mid-event disconnects every live socket (see
> [§4](#4-incident-escalation) severity rubric).

### 1b. DNS / TLS verification

```bash
# 🚧 Phase 6.1 landing needed.
export EVENT_URL=https://<event-domain>
dig +short "${EVENT_URL#https://}" | head        # Cloudflare edge IPs (104.x/172.x)
echo | openssl s_client -connect "${EVENT_URL#https://}:443" \
    -servername "${EVENT_URL#https://}" 2>/dev/null \
  | openssl x509 -noout -issuer -dates            # Let's Encrypt; spans event day
```

- [ ] DNS resolves to the Cloudflare edge (orange-cloud), not the raw VM IP.
- [ ] TLS cert issuer is Let's Encrypt and `notAfter` ≥ event day + a margin
      (renew now if it expires inside the event window). Cloudflare WebSockets
      toggle is **ON** (verify in the dashboard).

### 1c. Backup baseline (the rollback target)

Take a **named pre-event backup** immediately and ship it off-host. This is the
event-day rollback target — the restore drill proves it is restorable.

```bash
# On the prod VM, as the bench user (mechanics identical on the dev bench):
scripts/dev/backup.sh
# Record the archive path — this is THE event-day rollback target:
PRE_EVENT_BACKUP=$(ls -1d "$BENCH_DIR"/backups/"$SITE_NAME"-* | tail -1)
echo "Event-day rollback target: $PRE_EVENT_BACKUP"
```

- [ ] Pre-event backup archive path recorded in the event channel + the launch
      ticket. For prod, the off-host copy is confirmed (a backup on the same
      host as the DB is **not** a rollback target — see
      [`backup-restore.md` → Prod vs local](backup-restore.md#prod-vs-local)).
- [ ] The restore drill was green on the last prod-derived backup (signal S10).

### 1d. Secrets rotation confirmation

Confirm the SOPS+age secret bundles are current and the decrypt gate holds —
the **same** gate the deploy workflow runs before any rolling change.

```bash
# Off-image, no VM write needed:
SOPS_AGE_KEY_FILE=secrets/.age-key.prod scripts/deploy/render-secrets.sh --env prod --check
SOPS_AGE_KEY_FILE=secrets/.age-key.prod scripts/deploy/render-secrets.sh --env prod --out /tmp/flock.env
set -a; . /tmp/flock.env; set +a
scripts/deploy/render-config.sh --check
rm -f /tmp/flock.env   # plaintext — clean up immediately
```

- [ ] `render-secrets: --check OK` and `render-config: --check OK`. If a key
      is missing or the age key doesn't match, rotate before T-0 (see
      [`secrets-runbook.md`](../development/secrets-runbook.md)). Confirm the
      dedicated adapter-Redis secret is present.

### 1e. Stakeholder comms tree

Drafted by the CEO (business readiness, signal S-business). Capture before T-0:

- [ ] **On-call roster** — primary + secondary on-call for DevOps, plus the
      Architect as the escalation owner. Names + Paperclip agent ids + the
      comms channel (the event Paperclip thread).
- [ ] **Launch-partner point of contact** — the org's person who can authorise
      a pause (signal S-business: launch partner named + onboarded).
- [ ] **Attendee/leader comms template** filled in (see
      [§4 — comms](#comms-templates)) so a pause/rollback notice is one edit,
      not a drafting exercise mid-incident.

### 1f. Go/no-go re-walk (T-1d)

Re-walk [`launch-go-no-go.md`](launch-go-no-go.md) the day before. The pre-event
state changes most in the last 24h (a deploy, a cert renewal, a config drift).

- [ ] No deploy has landed on prod since the pre-event backup unless the
      post-deploy smoke is `PASS` **and** a fresh backup was taken.
- [ ] All of §1a–§1e are green. Any red → **no-go**; postpone or fall back to a
      simulated event.

---

## 2. T-0 — event-day monitoring

### 2a. What to watch (dashboards)

> The metrics pipeline/dashboard implementation is a separate Phase 6.2 slice
> (blocked on the staging env). The **metric names below are real** — they are
> already emitted by `flock_os.telemetry`, the WS k6 smoke, and the CEO monitor
> — but a consolidated Grafana/dashboard front-end is 🚧 **Phase 6.1 landing
> needed**. Until it lands, scrape these directly:

| Surface | Source | Key signals |
|---------|--------|-------------|
| **Realtime tier** | k6 `flock_ws_*` metrics + `supervisorctl status socketio-tier` | `flock_ws_connect_duration` p95, `flock_ws_broadcast_latency` p95, `flock_ws_receive_errors`, sessions-established % |
| **App + DB + Redis** | [`flock_os.telemetry`](../../flock_os/telemetry.py) `TelemetryCollector.snapshot()` | bulk-attendance p95, MariaDB slow-query count + buffer-pool hit ratio, Redis pub/sub `msg/s` + `connected_clients` + RQ depth |
| **Edge / rate-limit** | Cloudflare analytics + app limiter ([FLO-319](/FLO/issues/FLO-319)) | registration + realtime-connect request rate; limiter trip count |
| **Company liveness** | [`ceo-heartbeat-monitoring.md`](ceo-heartbeat-monitoring.md) (`scripts/dev/ceo-heartbeat-monitor.py --format prometheus`) | `ceo_health_severity`, silent-run alerts — a stuck CEO/agent mid-event is a single point of failure |

```bash
# 🚧 Phase 6.1 landing needed — a unified dashboard. Until then, the raw sources:
# App/DB/Redis snapshot (from the bench):
bench --site "$SITE_NAME" execute flock_os.telemetry.snapshot   # → the collector snapshot
# Realtime tier health:
docker exec flock-os supervisorctl status socketio-tier
# Company liveness (the most important non-event signal):
scripts/dev/ceo-heartbeat-monitor.py        # exit 0 ok / 1 warn / 2 critical
```

### 2b. SLO thresholds + watch cadence

These are the §8 targets from the launch gate, restated as **event-day SLOs**.
A breach is an incident — go to [§4](#4-incident-escalation).

| Signal | Green | Yellow (watch) | Red (page) |
|--------|-------|----------------|------------|
| WS connect p95 | < 1 s | 1–3 s | ≥ 3 s or climbing monotonically |
| WS broadcast p95 | < 1 s | 1–3 s | ≥ 3 s |
| `flock_ws_receive_errors` | 0 | 1–50 | > 50 or any sustained non-zero |
| Sessions established | 100% | 95–99% | < 95% |
| Bulk-attendance receipt p95 | < 500 ms (`BULK_LATENCY_P95_BUDGET_SECONDS`) | 500–1000 ms | ≥ 1 s |
| MariaDB buffer-pool hit ratio | > 99% | 95–99% | < 95% (D5 trigger — see [`telemetry.py`](../../flock_os/telemetry.py)) |
| Redis pub/sub saturation | < 50% | 50–80% | > 80% (D3 trigger — Redis cluster escape hatch) |

- **Doors-open ramp (first 10–15 min):** watch connect p95 + sessions-established
  every minute. This is where the connection-setup wall lives if the tier is
  mis-sized.
- **Steady-state (event live):** watch bulk-attendance p95, broadcast p95, and
  `flock_ws_receive_errors` every 5 min.
- **Engagement bursts (a game/questionnaire opens):** watch broadcast p95 +
  Redis pub/sub for the 60s around each kickoff.

### 2c. Who watches what

| Role | Watches | Cadence |
|------|---------|---------|
| **DevOps (primary on-call)** | realtime tier, app/DB/Redis telemetry, edge rate-limit | continuous during ramp; 5 min steady-state |
| **Backend (secondary)** | bulk-attendance p95, registration throughput, error logs | 5 min; on-call for an engagement burst |
| **CEO / ops lead** | attendee-facing experience, comms tree, the go/pause decision | continuous; owns the pause call |
| **Architect** | triage owner if an incident escalates past DevOps | on-page |

> **Rule #1 still holds on event day.** Never page a human for what an agent
> could do. The on-call DevOps agent handles triage; the CEO/Architect are
> paged only for a pause/rollback decision or a cross-cutting design failure.

---

## 3. Live attendance ops

The event-day feature loop, keyed off one **`Flock Gathering`** id:

```
Flock Gathering (the event)
   ├─ Flock Engagement Session  ← live games (tap_burst) / questionnaires (poll)
   ├─ Flock Attendance Record   ← attendance writes (bulk_submit hot path)
   ├─ Flock Event Registration  ← check-in (checked_in_count bump)
   ├─ realtime rooms            ← flock_os:event:<id>:(broadcast|shard:N)
   └─ scoped notifications      ← flock_os:notify:<axis>:<node>:broadcast
```

### 3a. Kick off a live game / questionnaire

Engagement is driven by **`Flock Engagement Session`** rows, opened/closed via
the `flock_os.engagement_api` whitelisted surface
(`open_session` / `join_session` / `close_session`). Engagement kinds map to
templates: `tap_burst` → `Flock Engagement Game Template`,
`poll`/`questionnaire` → `Flock Engagement Questionnaire Template`.

1. **Open the session** (the leader-facing compose UI calls
   `flock_os.engagement_api.open_session`). This persists the
   `Flock Engagement Session` row and arms it for joins.
2. **Confirm the realtime fan-out path is live** — clients join
   `flock_os:event:<gathering>:broadcast` + their `shard:N` room. Verify a test
   broadcast landed (see [§3c](#3c-confirm-attendance--registration-writes)).
3. **Close the session** (`close_session`) when the engagement window ends.
   Attendance/answers are captured as `Flock Attendance Record` /
   engagement-response rows regardless of close, but closing stops new joins.

> The scope gate (`flock_os.realtime_views.can_join_event_room` →
> `flock_os.permissions.can_access_branch`) is what keeps a client in its branch
> subtree. Do **not** try to "open" a session to the whole org by editing the
> gathering's branch — that is a row-level-perm anchor, not a broadcast switch
> (see [`realtime-edge-cases.md`](realtime-edge-cases.md) edge case #3).

### 3b. Confirm attendance + registration writes

The two hot write paths on event day, both already load-proven
([`scale-15k-findings.md`](scale-15k-findings.md)):

- **Bulk attendance** — `POST /api/method/flock_os.attendance.bulk_submit`
  (batched, idempotent, READ COMMITTED). ~5,000 rows/s on the local bench;
  its receipt p95 IS the §8 `< 500ms` SLO (the `measure_bulk_latency` histogram
  in [`flock_os.telemetry`](../../flock_os/telemetry.py)).
- **Registration check-in** — `check_in_registration` bumps
  `checked_in_count` via the **atomic single-statement UPDATE**
  `_bump_gathering_count(doc.gathering, "checked_in_count", +1)`
  ([`flock_event_registration.py:649`](../../flock_os/flock_os/doctype/flock_event_registration/flock_event_registration.py)).
  PERF-CHK-NONATOMIC from the scale drill is **closed by
  [FLO-515](/FLO/issues/FLO-515)** (`aa87d4d`, on this slice): it replaced the
  racy read-then-write with the atomic counter helper shared with
  `registered_count`, and the regression test
  `test_check_in_counter_is_atomic_update_not_read_then_write`
  ([`test_registrations.py:983`](../../flock_os/tests/test_registrations.py))
  pins it. Check-in count drift is therefore **no longer expected** — if you
  observe it under heavy concurrency, treat it as a **new counter regression,
  not a known benign race**: triage as [§4 Sev-2](#severity-rubric) and file a
  child issue immediately, rather than deferring it to the post-event retro.

### 3c. Confirm a broadcast landed (the 30-second check)

Run this once during ramp and once per engagement burst:

```bash
# Publish a count tick into the gathering's broadcast room; clients on the
# branch subtree should receive flock_os:attendance:count within the SLO.
bench --site "$SITE_NAME" execute \
  'frappe.publish_realtime("flock_os:attendance:count",
     message={"delta":1,"ts":int(__import__("time").time()*1000)},
     room="flock_os:event:<GATHERING-ID>:broadcast")'
```

If the tick reaches zero clients, the room-join path regressed — confirm the
join handler is wired (`scripts/dev/wire-socketio-handler.sh --check`) and the
gathering's branch matches the audience scope
([`ws-broadcast-delivery.md` → Verify end-to-end](../development/ws-broadcast-delivery.md#verify-end-to-end)).

### 3d. If a game session stalls

A stall is almost always one of three things. Triage in order:

| Symptom | Likely cause | Action |
|---------|--------------|--------|
| Clients connected but no broadcast reaching them | join handler dropped (a `bench update` rewrote `index.js`) | `scripts/dev/wire-socketio-handler.sh` then restart the realtime tier; the `after_migrate` hook should have caught this — if not, file a hardening issue |
| Broadcasts backing up (p95 climbing) | Redis pub/sub saturation (shared `redis_socketio`) | confirm the **dedicated adapter Redis** is wired (`FLOCK_SIO_ADAPTER_REDIS`); if it reverted to the shared instance, restart the tier with it set |
| Connections churning / sessions < 100% | the tier collapsed to a single process, or auth-callback wall returning | `supervisorctl status socketio-tier` — if 1 process, restart it (sees [§4 Sev-2](#severity-rubric)) |

> **Do not restart `socketio-tier` casually mid-event.** It disconnects every
> live socket and triggers a reconnect storm. The tier is self-healing across
> migrates; only restart it for a confirmed collapse (single process) or a
> stuck adapter, and announce the restart in the comms tree first.

### 3e. Trigger scoped notifications up the org tree

Admin → leader notifications use the **scoped fan-out** primitive
([FLO-57](/FLO/issues/FLO-57)): the announcement controller
(`flock_os.scheduling.publish_announcement`) resolves the audience **branch
subtree** and fans out via the same realtime path, one cheap publish per
org-tree node (room `flock_os:notify:<axis>:<node>:broadcast`, client event
`flock_os:notification`). A 15k-member subtree never fans out one-per-recipient.

```bash
# Compose → preview audience → publish (the FLO-60 compose UI calls these):
bench --site "$SITE_NAME" execute \
  'flock_os.scheduling.preview_audience' \
  --kwargs '{"organization":"<ORG>","branch":"<BRANCH>"}'   # → branch_count + subtree
# publish_announcement transitions the Flock Announcement Draft→Published + fans out
```

- The author-scope guard re-asserts the caller's branch scope server-side on
  every mutating call — a forged request cannot publish cross-subtree
  ([`realtime-edge-cases.md`](realtime-edge-cases.md) edge case #3).
- Notifications are **branch-subtree-only** by design; reaching the whole org
  requires publishing from the org root, not widening a single announcement.

---

## 4. Incident escalation

> This is the event-day incident section (folded in here per FLO-581 scope; it
> splits to its own doc only if it grows past ~300 lines). For general site
> troubleshooting — the platform services, blank pages, migration failures,
> permission scoping — use the [`mvp-operational-runbook.md` → Triage cheat
> sheet](mvp-operational-runbook.md#triage-cheat-sheet). **Do not duplicate it
> here.** This section is the event-day severity + paging + decision overlay.

### Severity rubric

| Sev | Definition | Event-day examples | Page | Target ack |
|-----|------------|--------------------|------|------------|
| **Sev-1** | Event-breaking / data loss / suspected breach | WS tier fully down; bulk-attendance failing; attendance data loss; security breach | DevOps primary **+ Architect + CEO** (critical) | immediate |
| **Sev-2** | Major degradation, event continues degraded | tier collapsed to 1 process; connect p95 ≥ 3s; sessions < 95%; broadcast p95 ≥ 3s sustained | DevOps primary (critical); Architect if not stabilised in 1 watch cycle | < 5 min |
| **Sev-3** | Limited degradation, watch | a single game session stall; bulk p95 in the yellow band; `receive_errors` 1–50 | DevOps primary (high) | next watch cycle |
| **Sev-4** | Cosmetic / non-urgent | a misformatted notification; a slow non-hot-path query | DevOps (medium) — log for the retro | post-event |

### The pause / rollback decision

This is the one decision the **CEO / ops lead** owns on event day (DevOps
prepares the rollback target; the CEO calls it). The rule of thumb:

- **Pause** (freeze new engagement sessions, keep the tier alive) when a Sev-2
  isn't stabilised inside one watch cycle or a Sev-1 is unfolding. A pause is
  reversible and preserves the live tier state.
- **Roll back** when data integrity is at risk (attendance writes failing
  non-idempotently, a bad deploy is the trigger, or the restore drill backup is
  the known-good state). Rolling back = `scripts/deploy/rollback.sh` to the
  pre-event image tag **plus** restore the pre-event backup if schema moved —
  follow [`migration-runbook.md` §5](migration-runbook.md#5-rollback-path) and
  [`deploy-runbook.md` → How to roll back](../development/deploy-runbook.md#how-to-roll-back).

> **Code + schema roll back together.** A schema rollback without the code
> rollback leaves the image expecting columns the restored dump removed. Always
> roll the image tag and the backup in the same step.

### Escalation chain (event-day overlay)

Same chain as [`mvp-operational-runbook.md` → Escalation path](mvp-operational-runbook.md#escalation-path),
tightened for event-day latency:

```
Sev-2/3 incident
   ▼
DevOps primary on-call  ── triage per §3d + the MVP triage cheat sheet
   │ not stabilised in one watch cycle
   ▼
Software Architect      ── owns system-design triage (realtime tier, perms chokepoint)
   │ Sev-1 or a pause/rollback call needed
   ▼
CEO / ops lead          ── owns the pause/rollback + attendee comms
   │ company-level (spend, public disclosure)
   ▼
Board
```

### Comms templates

Fill these in at T-7 ([§1e](#1e-stakeholder-comms-tree)) so an incident is one
edit. Keep them short — a long comms during an incident is a delay.

**Attendee/leader notice — pause:**
> We're briefly pausing live engagement to resolve a performance issue.
> Attendance is still being captured. We'll resume in approximately
> `<N>` minutes. — `<org>` team

**Attendee/leader notice — rollback/resolution:**
> Service is restored. `<Optional: any action attendees should take, e.g.
> rejoin the room>`. Thank you for your patience. — `<org>` team

**Internal (Paperclip event thread) — every incident:**
- Severity, time detected, signal that tripped it.
- Containment action + owner.
- Current state (degraded / paused / restored / rolled back).
- Next update time.

---

## 5. Post-event

### 5a. Stop the live surfaces

1. **Close every open `Flock Engagement Session`** for the gathering
   (`close_session`) so no new joins land.
2. **Confirm the gathering status** reflects the event end (`Flock Gathering`
   `status` lifecycle is in [`gatherings.py`](../../flock_os/gatherings.py)).
3. **Take a post-event backup** (same mechanics as [§1c](#1c-backup-baseline-the-rollback-target)).
   This is the artefact for the retrospective + the restore-drill input for the
   next release.

### 5b. Retrospective (Phase 6 acceptance)

The Phase 6 acceptance ([FLO-231](/FLO/issues/FLO-231) §2) closes on the
post-event retro: **real numbers**, not vibes. Copy this template into the
event ticket and fill it within 48h:

```markdown
## Event retro — <org>, <date>

### Real load vs SLOs
- Peak concurrency (live sockets): ______ (SLO target: ≤ 15k)
- Peak bulk-attendance rate: ______ rows/s (budget: ~5,000)
- Bulk-attendance receipt p95: ______ ms (SLO: < 500)
- WS connect p95: ______ s (SLO: < 1)
- WS broadcast p95: ______ s (SLO: < 1)
- flock_ws_receive_errors total: ______ (SLO: 0)
- Sessions established: ______ % (SLO: 100)
- Attendance capture rate (recorded / registered): ______ %

### What held
- <the paths that carried the event cleanly>

### What didn't
- <each incident: Sev, time, root cause, time-to-stabilise, link to Paperclip thread>

### Follow-ups
- [ ] <each Sev-1/Sev-2 gets a child issue; link them here>
- [ ] PERF-* items confirmed/resolved (PERF-CHK-NONATOMIC is closed by [FLO-515](/FLO/issues/FLO-515); any observed check-in drift should already be filed as a §4 Sev-2 child issue, not a retro item)

### Numbers vs the pre-event plan
- Expected vs actual peak, expected vs actual write rate, any capacity re-size done.
```

- A retro that surfaces a Sev-1/Sev-2 **files a child issue per incident** with
  the evidence; the parent Phase 6.4 ticket does not close with open Sev-1/2
  follow-ups.

### 5c. Backup confirmation + cost reconciliation

- [ ] **Backup:** post-event archive path recorded; an off-host copy confirmed;
      the next release's restore drill will run against it.
- [ ] **Cost reconciliation:** pointer to the hosting spend for the event
      window (the hosting-quote budget is ≈ $500–$1,500; actuals reconciled by
      the CEO against the approved budget). The DevOps contribution is the
      actuals read-out from the cloud bill — not the budget decision.

---

## Out of scope

- The **metrics pipeline / dashboard implementation** — separate Phase 6.2
  slice, blocked on the staging env (FLO-266 shipped the metric sources; the
  unified dashboard front-end is 🚧 Phase 6.1 landing needed).
- The **deploy / rollback orchestrator mechanics** —
  [`deploy-runbook.md`](../development/deploy-runbook.md).
- **Backup/restore drill mechanics + retention** —
  [`backup-restore.md`](backup-restore.md).
- **Migration flow** — [`migration-runbook.md`](migration-runbook.md).
- **General site triage** (platform services, blank pages, perm scoping,
  migration failures) —
  [`mvp-operational-runbook.md`](mvp-operational-runbook.md).
- **Realtime tier design + edge cases** —
  [`ws-broadcast-delivery.md`](../development/ws-broadcast-delivery.md) +
  [`realtime-edge-cases.md`](realtime-edge-cases.md).
- **Launch go/no-go gate definition** — [`launch-go-no-go.md`](launch-go-no-go.md).
- **Sibling doc cross-links not yet on `master`** —
  [`scale-15k-findings.md`](scale-15k-findings.md)
  ([FLO-365](/FLO/issues/FLO-365)), [`launch-go-no-go.md`](launch-go-no-go.md)
  ([FLO-332](/FLO/issues/FLO-332)/[FLO-357](/FLO/issues/FLO-357)), and
  [`migration-runbook.md`](migration-runbook.md)
  ([FLO-332](/FLO/issues/FLO-332)/[FLO-350](/FLO/issues/FLO-350)) land on their
  own Phase 6.1/6.2 slices; links resolve once those merge to master.
- The **incident-response runbook as a separate doc** — folded into §4 for now;
  splits only if §4 grows past ~300 lines.

## Related

- Parent strategy: [FLO-231](/FLO/issues/FLO-231) (Phase 6 — Production Launch).
- Phase 6.2 parent: [FLO-533](/FLO/issues/FLO-533) (this runbook is acceptance criterion #3).
- This slice: [FLO-581](/FLO/issues/FLO-581) (endorsement-independent).
- Precedent (endorsement-agnostic runbook): [FLO-544](/FLO/issues/FLO-544).
- Observability metric sources: [FLO-266](/FLO/issues/FLO-266) (done) +
  [`flock_os.telemetry`](../../flock_os/telemetry.py).
- Deploy / rollback: [`docs/development/deploy-runbook.md`](../development/deploy-runbook.md).
- Backup & restore: [`backup-restore.md`](backup-restore.md).
- Migration: [`migration-runbook.md`](migration-runbook.md).
- MVP triage + escalation: [`mvp-operational-runbook.md`](mvp-operational-runbook.md).
- Realtime design + edge cases: [`ws-broadcast-delivery.md`](../development/ws-broadcast-delivery.md) +
  [`realtime-edge-cases.md`](realtime-edge-cases.md).
- Launch gate: [`launch-go-no-go.md`](launch-go-no-go.md).
- 15k data-tier profile: [`scale-15k-findings.md`](scale-15k-findings.md).

## Change log

| Date | Issue | Change |
| --- | --- | --- |
| 2026-06-21 | [FLO-581](/FLO/issues/FLO-581) | Initial event-day runbook (T-7 → T-0 → live ops → incident → post-event). Endorsement-independent; prod-infra steps flagged. |
| 2026-06-21 | [FLO-583](/FLO/issues/FLO-583) | Architect review re-draft: §3b `PERF-CHK-NONATOMIC` note rewritten — drift is now a [§4 Sev-2](#severity-rubric) regression, not a benign race, since [FLO-515](/FLO/issues/FLO-515) shipped the atomic `checked_in_count` UPDATE (retro checklist aligned); §0 + Out-of-scope note the three sibling-doc cross-links that resolve on [FLO-365](/FLO/issues/FLO-365)/[FLO-332](/FLO/issues/FLO-332)/[FLO-350](/FLO/issues/FLO-350). |
