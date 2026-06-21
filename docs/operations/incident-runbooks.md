# Incident response runbooks — Flock OS (FLO-694 Phase 6.2)

> **Definition owner:** [FLO-694](/FLO/issues/FLO-694) (Phase 6.2 — observability
> runbooks). **Parent:** [FLO-231](/FLO/issues/FLO-231) (Phase 6 strategy).
> **Sibling:** [FLO-533](/FLO/issues/FLO-533) (full observability epic).
>
> This is the **procedure layer** — what the on-call engineer *does* when an
> alert from the [metrics + alerting design](metrics-alerting-design.md) §3.4
> routing table fires. The [metrics design](metrics-alerting-design.md) owns
> *detection* (thresholds, paging); this doc owns *response*. The two are
> deliberately separate so alerting can be retuned without rewriting procedure,
> and vice-versa (same split rationale as the [event-day runbook](event-day-runbook.md)).
>
> **VM-independent:** every procedure below is no-spend design — it names the
> commands, the panels, and the decision points so that when the prod VM is up
> ([FLO-249](/FLO/issues/FLO-249)) the on-call executes from a proven script
> rather than improvising under pressure. Commands presume a `bench`-shaped host
> (identical mechanics on the dev bench and in prod — see
> [backup-restore.md](backup-restore.md) "VM-independent").

## TL;DR

- **Six named incidents.** WS connection storm, adapter Redis failover, MariaDB
  deadlock/lock storm, 15k-event degradation, deploy rollback, secret rotation.
  The first four live here; **deploy rollback** and **secret rotation** have
  their own runbooks ([deploy-runbook](../development/deploy-runbook.md) §"How to
  roll back", [secrets-runbook](../development/secrets-runbook.md) §"Rotate the
  age key") and are cross-linked from §5–§6.
- **Every incident opens the [incident-triage view](metrics-alerting-design.md#23-incident-triage-view)
  first.** The correlated-tier panel stack localizes which tier moved first — do
  not start typing commands before the triage view tells you where to look.
- **Severity + paging live in the [metrics design §3](metrics-alerting-design.md#3-alerting--thresholds-paging-escalation),
  not here.** This doc assumes you have already been paged (critical) or seen
  the `#ops` post (warning) and are now executing response.
- **The pause/rollback decision is named per incident.** Each procedure has a
  "When to pause/rollback" line — the bar to stop the event vs continue degraded.

## 0. Universal triage — open this first

For **any** critical alert:

1. **Ack the page** (PagerDuty) within the [SLA](metrics-alerting-design.md#32-paging-policy)
   — ack < 1 min in event window. Acking stops the escalation timer; it does
   not mean the incident is over.
2. **Open the [incident-triage view](metrics-alerting-design.md#23-incident-triage-view).**
   Read the **correlated tier view** top-to-bottom: which tier moved first? The
   "recent-deploy marker" vertical line — did this start within ~30 min of a
   promotion? If yes, jump to [§5 deploy rollback](#5-deploy-rollback).
3. **Post in `#ops`** that you are the on-call and have the page (single-channel
   principle — the warning→critical promotion must be visible in thread).
4. **Identify the incident** from the firing alert(s) — match against the table
   below — and jump to that section.

| Firing alert(s) | Incident | Section |
| --- | --- | --- |
| `WSConnectSLOBreach` / `WSBroadcastSLOBreach` / `WSErrorCounterNonZero` / `WSessionsDropped` / sticky-LB miss rate critical | WS connection storm | [§1](#1-ws-connection-storm) |
| `AdapterRedisNearMaxMemory` / `AdapterRedisEvicting` / `AdapterSubscribersLost` | Adapter Redis failover | [§2](#2-adapter-redis-failover) |
| `db.deadlocks` > 0/s / `db.lock_waits` critical / `DBConnectionExhaustion` | MariaDB deadlock / lock storm | [§3](#3-mariadb-deadlock--lock-storm) |
| compound — multiple §8 [scale signals](launch-go-no-go.md#scale--the-8-targets-reproduced-at-15k-flo-347) drifting in-event | 15k-event degradation | [§4](#4-15k-event-degradation) |
| incident starts ≤ 30 min after a promotion | Deploy rollback | [§5](#5-deploy-rollback) |
| scheduled / post-compromise / age-key rotation | Secret rotation | [§6](#6-secret-rotation) |

## 1. WS connection storm

> **Fires on:** `WSConnectSLOBreach`, `WSBroadcastSLOBreach`,
> `WSErrorCounterNonZero`, `WSessionsDropped`, or the sticky-LB
> `ws.lb.affinity_miss_rate` critical (> 20%). The §8 WS SLOs
> ([signals S4–S7](launch-go-no-go.md#scale--the-8-targets-reproduced-at-15k-flo-347))
> are the canary — these are [no-go #9](launch-go-no-go.md#no-go-conditions) if
> they trip at the launch gate.
>
> **Why this is first:** the WS tier is the dominant scale axis
> ([FLO-121](/FLO/issues/FLO-121) scaled socketio, [FLO-127](/FLO/issues/FLO-127)
> dedicated adapter Redis). A WS storm left unchecked cascades into the
> auth-callback wall ([FLO-116](/FLO/issues/FLO-116)) and the broadcast-drop
> symptom ([FLO-107](/FLO/issues/FLO-107)).

### 1a. Triage (≤ 2 min — order matters)

1. **Per-worker distribution** — `ws.connections.per_worker` panel on the triage
   view. Is one worker at 0, or one at > 2× the mean? → a worker died or the
   sticky hash skewed ([FLO-121](/FLO/issues/FLO-121) round-robin signature broke).
2. **Affinity miss rate** — `ws.lb.affinity_miss_rate`. If > 20%, the LB hash is
   wrong → every reconnect falls back to the per-connection HTTP auth callback
   (the [FLO-116](/FLO/issues/FLO-116) wall). This is the fastest cascading path.
3. **Adapter Redis** — `redis.pubsub_numsub_socketio` < N−1? A worker's adapter
   subscriber is down → cross-worker broadcasts gap. Jump to [§2](#2-adapter-redis-failover).
4. **Connect vs disconnect rate** — `ws.disconnect_rate` > 500/s is a mass
   disconnect (LB restart, adapter blip, or auth-cache invalidation storm).
   `ws.connect_rate` > 100/s sustained is the event-opening burst (expected at
   T-0, alarming mid-event).

### 1b. Immediate actions (stabilize)

- **Dead/overloaded worker:** restart the single socketio worker process
  (supervisor: `supervisorctl restart socketio-tier:<worker-name>`; bare bench:
  identify + restart the node). Do **not** bounce the whole tier unless > 1
  worker is down — a full tier restart triggers a mass reconnect burst.
- **Affinity miss storm:** the LB hash config drifted. Re-assert the sticky
  upstream-hash on `sid` in `deploy/nginx/prod.conf` and `nginx -s reload`
  (not a full restart). See [deploy-runbook §"nginx sticky-L7"](../development/deploy-runbook.md#nginx-sticky-l7-cloudflare-caveat).
- **Mass disconnect with no single-worker cause:** check the adapter Redis first
  ([§2](#2-adapter-redis-failover)) — most mass-disconnects trace to the adapter,
  not the workers.

### 1c. When to pause/rollback the event

- **Pause (stop new joins, keep existing sessions):** `WSConnectSLOBreach`
  sustained > 2 min, OR `WSErrorCounterNonZero` non-zero for > 1 min. Pausing =
  hold the registration + realtime-connect paths at the edge (the
  [FLO-294](/FLO/issues/FLO-294) / [FLO-319](/FLO/issues/FLO-319) rate limit is
  the lever — tighten the cap temporarily). Existing attendees keep their
  sessions; new joins queue.
- **Rollback (revert the promotion):** if the storm started ≤ 30 min after a
  deploy. Go to [§5](#5-deploy-rollback). A WS regression on a clean
  pre-deploy tier points to the deploy, not organic load.

### 1d. Root-cause (post-stabilize)

- Pull the last 20 `flock_ws_receive_errors` from the k6/continuous probe + the
  worker `/metrics` scrape around the firing window.
- Re-run [`scripts/dev/scale-socketio.sh --lb nginx`](../../scripts/dev/) at
  reduced load post-event to reproduce; the full 15k reproduction is a
  launch-gate exercise, not an in-event one.

## 2. Adapter Redis failover

> **Fires on:** `AdapterRedisNearMaxMemory` (`used_memory_rss` > 90% `maxmemory`),
> `AdapterRedisEvicting` (`evicted_keys` > 0/s — policy drift under `noeviction`),
> `AdapterSubscribersLost` (`pubsub_numsub_socketio` < N−1). These are
> [no-go #6](launch-go-no-go.md#no-go-conditions) at the gate (dedicated adapter
> Redis degraded).
>
> **Why this is high-stakes:** the adapter Redis is **the** WS-scale bottleneck
> ([FLO-127](/FLO/issues/FLO-127)). It fans every broadcast to every worker. A
> degraded adapter silently drops broadcasts — attendees see stale counts, the
> [FLO-107](/FLO/issues/FLO-107) "broadcasts reached zero clients" symptom —
> before any connection-count metric moves.

### 2a. Triage (≤ 2 min)

1. **Which Redis?** Confirm this is the **adapter** Redis, not the cache Redis.
   The [metrics design §1.3](metrics-alerting-design.md#13-redis-tier--dedicated-socketio-adapter-the-ws-scale-bottleneck)
   monitors the adapter only; the cache Redis is commodity. If the page names
   the adapter instance, proceed.
2. **Memory pressure vs config drift:** `AdapterRedisNearMaxMemory` = working
   set grew (room-tracking keys for live events). `AdapterRedisEvicting` =
   **config drift** — production runs `maxmemory-policy=noeviction`, so *any*
   eviction means someone changed the policy. `CONFIG GET maxmemory-policy` to
   confirm; if it is not `noeviction`, that is the root cause.
3. **Subscriber count:** `redis.pubsub_numsub_socketio` < N means a worker's
   adapter subscriber disconnected. Cross-correlate with §1a per-worker
   distribution — same root cause, different symptom.

### 2b. Immediate actions (stabilize)

- **Config drift (`AdapterRedisEvicting`):** restore `noeviction` immediately:
  `redis-cli -h <adapter-host> -p <port> CONFIG SET maxmemory-policy noeviction`.
  Then find who changed it (deploy diff / runbook drift). Eviction under
  `noeviction` also means writes are *failing* — broadcasts were dropped.
- **Memory pressure (`AdapterRedisNearMaxMemory`):** raise `maxmemory` if
  headroom exists on the host (`CONFIG SET maxmemory <bytes>`), else shed load
  by [pausing new joins](#1c-when-to-pauserollback-the-event) (the room-tracking
  keys scale with active rooms). A long-term fix is a larger adapter instance —
  out of scope for in-event response.
- **Subscriber lost (`AdapterSubscribersLost`):** the worker whose adapter
  subscriber dropped needs a restart (§1b single-worker restart). The adapter
  reconnects on worker start.

### 2c. When to pause/rollback the event

- **Pause:** any adapter Redis critical. Broadcast reliability is the floor
  under attendance accuracy — a dropping adapter silently corrupts the
  attendance count. Pause new joins until `pubsub_numsub_socketio` == N and
  eviction rate == 0 for 2 min.
- **Rollback:** `AdapterRedisEvicting` traced to a config change in the last
  deploy → [§5](#5-deploy-rollback).

### 2d. Root-cause (post-stabilize)

- `MEMORY DOCTOR` + `INFO memory` on the adapter for the working-set breakdown.
- If `AdapterSubscribersLost` recurs, the worker's adapter keepalive is
  timing out — check the `@socket.io/redis-adapter` pingInterval vs the
  Redis `timeout` setting ([FLO-121](/FLO/issues/FLO-121) adapter wiring).

## 3. MariaDB deadlock / lock storm

> **Fires on:** `db.deadlocks` > 0/s (any deadlock in the bulk-write path is a
> correctness signal), `db.lock_waits` > 20/s for 2 min, or
> `DBConnectionExhaustion` (active > 90% pool). See
> [metrics design §1.2](metrics-alerting-design.md#12-db-tier--managed-mariadb).
>
> **Why this matters:** the bulk-attendance write path
> ([FLO-15](/FLO/issues/FLO-15)) is the DB hotspot. The
> [PERF-BULK-FORUPDATE](scale-15k-findings.md#high) class — per-member
> `SELECT … FOR UPDATE` serializing a bulk batch — is the known regression
> shape; a deadlock storm usually means it returned or a new write path
> skipped the batch pattern.

### 3a. Triage (≤ 2 min)

1. **Deadlock or lock-wait?** `SHOW ENGINE INNODB STATUS` → the `LATEST DETECTED
   DEADLOCK` section names the two transactions + the SQL each held. The
   `LATEST FOREIGN KEY ERROR` and `TRANSACTIONS` sections show long-running
   locks.
2. **Which write path?** Cross-correlate with `http.request.duration_ms` for the
   `attendance` route class. A p95 spike coincident with the deadlock storm =
   the bulk-attendance path. A spike on `register` = the registration path.
3. **Connection exhaustion vs deadlock:** `DBConnectionExhaustion` without a
   deadlock storm is a connection leak (a query path not releasing), not a lock
   issue. Different fix — see §3b last bullet.

### 3b. Immediate actions (stabilize)

- **Deadlock storm in the bulk path:** the immediate lever is to shed bulk-write
  load — pause the bulk-attendance ingest (the RQ `long` queue is the source).
  `rq info` to confirm the queue is the source; the queue depth alert
  (`rq.queue_depth` > 10,000) will have fired alongside.
- **Long-running lock holder:** identify the offending transaction from
  `information_schema.innodb_trx` (`SELECT trx_id, trx_started, trx_query FROM
  information_schema.innodb_trx ORDER BY trx_started ASC LIMIT 5;`). The oldest
  is the blocker. Kill it (`KILL <trx_mysql_thread_id>;`) only if it is a stuck
  bulk batch, not a legitimate long report query.
- **Connection leak (`DBConnectionExhaustion`):** restart the gunicorn workers
  (`supervisorctl restart web` / `bench restart`) to release leaked
  connections. Then find the leak from the slow-query / connection-source log.

### 3c. When to pause/rollback the event

- **Pause:** deadlock storm sustained > 2 min OR `DBConnectionExhaustion`. The
  bulk-write path is corrupting attendance under deadlock — pause ingest, let
  the queue drain slowly, and investigate. Existing sessions keep working
  (reads + single-row writes are unaffected).
- **Rollback:** if the deadlock traces to a query-plan regression introduced in
  the last deploy (the [PERF-BULK-FORUPDATE](scale-15k-findings.md#high) shape
  re-introduced) → [§5](#5-deploy-rollback).

### 3d. Root-cause (post-stabilize)

- Capture `EXPLAIN` for the deadlocking SQL against the prod-shaped data via
  [`scripts/dev/profile-15k-scale.sh`](../../scripts/dev/) — the index
  regression that produces `FOR UPDATE` serialization shows as a full scan on
  the member table.
- File a PERF follow-up if the fix is non-trivial; do not hold the event for a
  query-plan rewrite.

## 4. 15k-event degradation

> **Fires on:** compound — multiple [§8 scale signals](launch-go-no-go.md#scale--the-8-targets-reproduced-at-15k-flo-347)
> drifting simultaneously mid-event: WS connect p95 creeping past 500 ms
> (warning), broadcast p95 past 500 ms, `rq.queue_depth` climbing, DB lock-waits
> rising. No single alert; the on-call reads the [event-day ops board](metrics-alerting-design.md#21-event-day-real-time-ops-board-the-on-call-screen)
> headline strip and sees amber across 2+ SLOs.
>
> **Why this is distinct from §1–§3:** those incidents have one tier as the
> clear first-mover. 15k-event degradation is **capacity exhaustion across
> tiers** — the prod tier is undersized for the load. The fix is load-shedding +
> capacity, not a single bad component.

### 4a. Triage (≤ 3 min)

1. **Which SLOs are amber/red?** Read the headline strip in order: WS connect
   p95, WS broadcast p95, `http.5xx`, `flock_ws_receive_errors`, then the
   supporting panels (per-worker, adapter Redis, DB pressure).
2. **Is one tier the first-mover?** If yes, this is really §1/§2/§3 — handle it
   there. §4 applies when **no single tier is the first-mover** — all are
   degrading together.
3. **Actual concurrency vs the 15k budget** — `ws.connections.active` against
   15,000. If the event has over-sold vs the load-proven budget, this is
   organic overload, not a bug.

### 4b. Immediate actions (stabilize — load-shed)

- **Tighten the edge rate limit** on registration + realtime-connect
  ([FLO-294](/FLO/issues/FLO-294) / [FLO-319](/FLO/issues/FLO-319)). The goal is
  to cap *new* join rate below the provisioned connect budget, letting the
  already-connected attendees continue. This is the single most effective
  in-event lever.
- **Shed bulk background work** — suspend the RQ `long` queue (bulk reporting,
  non-attendance bulk jobs) to free DB + worker capacity for the live path:
  `rq suspend` on the `long` queue. Resume post-event.
- **Do not** restart the WS tier mid-event for a capacity issue — a restart
  triggers a reconnect burst that worsens the overload. Restart is for a dead
  worker (§1), not capacity.

### 4c. When to pause/rollback the event

- **Pause:** WS connect OR broadcast p95 at critical (> 1 s) for > 2 min after
  load-shedding. The event cannot guarantee delivery above the SLO ceiling.
  Pause = hold new joins; the on-call + event owner decide whether to resume
  after the peak passes.
- **Rollback:** only if the degradation traces to a deploy (recent-deploy marker
  aligns). Capacity exhaustion on a clean tier is not a deploy problem → do not
  roll back; instead file a capacity follow-up ([FLO-245](/FLO/issues/FLO-245)
  topology) for the next event.

### 4d. Root-cause (post-event)

- This is primarily a retrospective input ([event-day-runbook §5b](event-day-runbook.md#5b-retrospective-phase-6-acceptance)):
  record the actual peak concurrency, the SLO breach pattern, and whether the
  provisioned tier matched the [hosting-quote](/FLO/issues/FLO-231#document-hosting-quote)
  envelope. The fix is almost always capacity (a larger adapter Redis, more
  socketio workers, a bigger DB pool), not code.

## 5. Deploy rollback

> **Full procedure:** [`docs/development/deploy-runbook.md` §"How to roll back"](../development/deploy-runbook.md#how-to-roll-back).
> This section is the **incident-trigger summary** — when a rollback is the
> incident response, not a planned promotion.

**Rollback is the incident response when:**

- The [incident-triage view](metrics-alerting-design.md#23-incident-triage-view)
  recent-deploy marker aligns with the incident start (≤ 30 min), **and**
- The failing tier was healthy pre-deploy (triage view history confirms), **and**
- §1–§4 immediate actions are not stabilizing.

**Do NOT rollback when:**

- The incident is capacity exhaustion (§4) on a clean tier — rollback does not
  add capacity.
- The incident is a single dead worker (§1) — restart the worker, do not
  revert the deploy.

**The rollback target:** the pre-deploy backup recorded in the
[event-day runbook §1c backup baseline](event-day-runbook.md#1c-backup-baseline-the-rollback-target)
(event-day) or the deploy-pipeline's pre-promotion backup (standard). See
[backup-restore.md](backup-restore.md) for the restore mechanics + the
[deploy-runbook](../development/deploy-runbook.md) for the promotion revert.

## 6. Secret rotation

> **Full procedure:** [`docs/development/secrets-runbook.md` §"Rotate the age key"](../development/secrets-runbook.md#rotate-the-age-key-compromise--scheduled)
> (compromise / scheduled) and §"Rotate a single secret value" (one value). This
> section is the **incident-trigger summary**.

**Secret rotation is the incident response when:**

- A secret is suspected compromised (age private key, DB root password, admin
  password) — rotate **before** investigating, per the [pre-production audit §3](../security/pre-production-audit.md)
  zero-hardcoded-secrets posture.
- A scheduled rotation falls due (the [event-day runbook §1d](event-day-runbook.md#1d-secrets-rotation-confirmation)
  T-7 checklist confirms rotation is current before the event).

**Rotation order (the age-key case — most disruptive):**

1. Generate a fresh keypair + store the private key out-of-band (password
   manager + GitHub environment secret) — [secrets-runbook §"Bootstrap + rotate"](../development/secrets-runbook.md#bootstrap--rotate-the-age-key).
2. `sops --rotate` to re-wrap the data key for the new recipient (values
   untouched).
3. Rotate individual compromised values (DB creds etc.) at their source
   (managed-DB console / Frappe user manager), then update the bundle.
4. Re-render + redeploy: `render-config --check` green, then the deploy pipeline.

**The lost-key emergency** (no access to the private key) is a full re-encrypt
from scratch — see [secrets-runbook §"Emergency: lost private key"](../development/secrets-runbook.md#emergency-lost-private-key).

## 7. Post-incident (every incident)

1. **File the incident record** — a tracking issue summarizing: trigger alert(s),
   detection-to-ack time, detection-to-resolution time, the root cause, the
   stabilization action, and any load-shed/pause/rollback decision.
2. **Link the retrospective** to the [event-day runbook §5b](event-day-runbook.md#5b-retrospective-phase-6-acceptance)
   if the incident occurred in an event window.
3. **Update this doc + the [metrics design](metrics-alerting-design.md)** if the
   incident revealed a threshold that was too loose/tight or a procedure step
   that did not work. The detection/response split means these edit
   independently.
4. **Re-arm the alert** — confirm the firing alert returned to green before
   closing the incident.

## 8. Out of scope

- **Alert thresholds + paging policy** — owned by the
  [metrics design §3](metrics-alerting-design.md#3-alerting--thresholds-paging-escalation).
- **The event-day timeline procedure** (T-7 → T-0 → T+24h) — owned by the
  [event-day runbook](event-day-runbook.md). This doc is the per-incident
  response layer the event-day runbook escalates into.
- **Backup/restore mechanics** — owned by [backup-restore.md](backup-restore.md).
  Referenced here as the rollback-target path.
- **Deploy + rollback mechanics** — owned by the
  [deploy-runbook](../development/deploy-runbook.md). Referenced here as the
  incident-response rollback trigger.
- **The security posture itself** — owned by the
  [pre-production audit](../security/pre-production-audit.md). Secret rotation
  (§6) is the procedural response; the audit is the posture.

## 9. Related

- Parent strategy: [FLO-231](/FLO/issues/FLO-231) (Phase 6 — Production Launch).
- Owner: [FLO-694](/FLO/issues/FLO-694) (Phase 6.2 — observability runbooks).
- Sibling epic: [FLO-533](/FLO/issues/FLO-533) (Phase 6.2 — observability, security & ops).
- Detection layer (thresholds, paging, alert routing): [`metrics-alerting-design.md`](metrics-alerting-design.md) ([FLO-586](/FLO/issues/FLO-586)).
- Event-day timeline procedure: [`event-day-runbook.md`](event-day-runbook.md) ([FLO-581](/FLO/issues/FLO-581)).
- Launch go/no-go gate (signals S4–S8, no-go conditions): [`launch-go-no-go.md`](launch-go-no-go.md) ([FLO-357](/FLO/issues/FLO-357)).
- Backup + restore (the rollback target): [`backup-restore.md`](backup-restore.md) ([FLO-288](/FLO/issues/FLO-288)).
- Deploy / rollback mechanics: [`docs/development/deploy-runbook.md`](../development/deploy-runbook.md) ([FLO-246](/FLO/issues/FLO-246)).
- Secret rotation mechanics: [`docs/development/secrets-runbook.md`](../development/secrets-runbook.md) ([FLO-248](/FLO/issues/FLO-248)).
- Security posture: [`docs/security/pre-production-audit.md`](../security/pre-production-audit.md) ([FLO-682](/FLO/issues/FLO-682)).
- WS scale tier (the dominant axis): [FLO-121](/FLO/issues/FLO-121), [FLO-127](/FLO/issues/FLO-127), [FLO-116](/FLO/issues/FLO-116), [FLO-107](/FLO/issues/FLO-107); runbook [`ws-broadcast-delivery.md`](../development/ws-broadcast-delivery.md).
- 15k load profile (the threshold source): [FLO-365](/FLO/issues/FLO-365) [findings](scale-15k-findings.md).
