# Event-day operations runbook — Flock OS (FLO-895 / FLO-581 — Phase 6.2)

> **Definition owner:** [FLO-895](/FLO/issues/FLO-895) (Phase 6.2 — operations
> runbooks). **Cross-owner:** [FLO-581](/FLO/issues/FLO-581) (the event-day
> procedure layer the [metrics + alerting design](metrics-alerting-design.md)
> §3.3 routes critical alerts into). **Parent epic:**
> [FLO-533](/FLO/issues/FLO-533) (Phase 6.2 — observability, security & ops).
> **Strategy:** [FLO-231](/FLO/issues/FLO-231) (Phase 6 — Production Launch).
>
> This is the **timeline layer** — what the on-call engineer *does* across the
> T-7 → T-0 → T+24h arc of a live event day. It owns the pre-event checklist,
> the during-event watch posture, and the post-event teardown. The per-incident
> *technical response* (which button to push when an alert fires) lives in the
> sibling [`incident-runbooks.md`](incident-runbooks.md) ([FLO-694](/FLO/issues/FLO-694));
> the *detection* (thresholds, paging) lives in the
> [metrics design §3](metrics-alerting-design.md#3-alerting--thresholds-paging-escalation)
> ([FLO-586](/FLO/issues/FLO-586)). The three artifacts are deliberately split
> so each can be retuned independently.
>
> **VM-independent:** every step below names the commands, the panels, and the
> decision points so that when the prod VM is up ([FLO-249](/FLO/issues/FLO-249))
> the on-call executes from a proven script. The event-day load itself is
> reproduced locally by [`scripts/dev/event_day.sh`](../../scripts/dev/event_day.sh)
> ([FLO-698](/FLO/issues/FLO-698)) — registration + WS fan-out + bulk attendance
> run in parallel as one composite SLO gate.

## TL;DR

- **Three phases, one owner.** T-7 (scale-up + go/no-go walk), T-0 (watch +
  respond), T+24h (teardown + retrospective). The on-call engineer owns all
  three; the event owner owns the business go.
- **The on-call screen is [`event-day-ops.json`](dashboards/event-day-ops.json)**
  (last 15 min, LIVE) — open it at T-0 and keep it open. The headline SLO strip
  is the at-a-glance health; the correlated tier panels are the triage path.
- **Every critical alert deep-links into this runbook + [`incident-runbooks.md`](incident-runbooks.md).**
  You do not triage from scratch — the alert names the section.
- **The pause lever is the edge rate limit** ([FLO-294](/FLO/issues/FLO-294) /
  [FLO-319](/FLO/issues/FLO-319)) — tighten it to hold new joins while existing
  sessions keep working. Pause ≠ stop; it is the single most effective in-event
  stabilization action.
- **Rollback target is the §1c backup baseline.** If the event breaks on a
  deploy, the rollback target is the pre-event backup recorded at T-7.

## 0. Roles & on-call roster

| Role | Who | Owns |
|------|-----|------|
| **On-call engineer** | Rotated; named in the PagerDuty schedule for the event window | T-7 → T+24h procedure; acks pages; runs stabilization |
| **Event owner** | Named per event (the launch partner lead) | Business go/no-go; attendee comms; the "pause the event" business call |
| **Incident commander** | The on-call, or a second engineer paged in for Sev1 | Coordinates response when an incident escalates — see [`incident.md`](incident.md) |
| **Deploy owner** | DevOps | The deploy freeze window (T-2 → T+24h); any emergency promotion |

> **Roster naming.** The [metrics design §3.2 paging policy](metrics-alerting-design.md#32-paging-policy)
> pages "the on-call engineer (rotated, named in the event-day runbook roster)".
> Record the named on-call + event owner + their PagerDuty schedule IDs in the
> event's tracking issue at T-7. Until the prod VM is up, the roster is a role
> assignment, not a live page.

## 1. T-7 — the week before (scale-up + readiness)

The goal of T-7 is to prove the tier is sized for the event **before** traffic,
so T-0 is watch-and-respond, not bring-up-and-debug.

### 1a. Tier scale-up confirmation

Confirm the prod tier matches the load-proven shape. The 15k gate
([FLO-347](/FLO/issues/FLO-347)) is the reference; the event's expected peak
concurrency sets the sizing.

- [ ] **Realtime tier process count == N** (not collapsed to 1):
      `docker exec flock-os supervisorctl status socketio-tier`
      (no-go #3 / signal S8 — see [`launch-go-no-go.md`](launch-go-no-go.md)).
- [ ] **Adapter Redis** is the dedicated instance, `maxmemory-policy=noeviction`,
      headroom > 20%: `redis-cli -h <adapter> CONFIG GET maxmemory-policy`
      (no-go #6).
- [ ] **MariaDB** connection pool sized to the 15k budget; `max_connections`
      leaves ≥ 20% headroom over peak active.
- [ ] **nginx sticky-L7** hash on `sid` is active (`deploy/nginx/prod.conf`);
      `nginx -t` green. If Cloudflare-fronted, the sticky-cookie variant is in
      place — see [deploy-runbook §"nginx sticky-L7"](../development/deploy-runbook.md#nginx-sticky-l7-cloudflare-caveat).
- [ ] **Edge rate limit** caps set per [FLO-294](/FLO/issues/FLO-294) /
      [FLO-319](/FLO/issues/FLO-319); confirm the pause lever (tightening the
      cap) is a known config edit, not a code change.

If the event's expected peak exceeds the 15k load-proven budget, this is a
capacity follow-up ([FLO-245](/FLO/issues/FLO-245) topology), not an in-event
fix — raise it now, not at T-0.

### 1b. Go/no-go gate walk

Walk the [`launch-go-no-go.md`](launch-go-no-go.md) checklist with DevOps + QA +
CEO + Architect. Every box checked = go; any no-go condition = fix before T-0.
The four signatures (DevOps deploy, QA smoke/coverage, CEO business, Architect
topology) are the gate. Run [`scripts/launch-gate.sh`](../../scripts/launch-gate.sh)
([FLO-354](/FLO/issues/FLO-354)) to machine-check the repo-local rows.

### 1c. Backup baseline (the rollback target)

Record the rollback target **before** the event. This is the backup a
deploy-rollback or DB-restore returns to (referenced from
[`incident-runbooks.md` §5](incident-runbooks.md#5-deploy-rollback)).

```bash
# Take the pre-event baseline (archive lands under $BENCH_DIR/backups/<site>-<ts>/):
scripts/ops/backup.sh

# Record the archive path + the current image tag in the event tracking issue:
ARCHIVE=$(ls -1d $BENCH_DIR/backups/flock_os.localhost-* | tail -1)
echo "rollback target: $ARCHIVE ; image tag: $(git rev-parse --short HEAD)"
```

- [ ] Baseline backup taken; archive path recorded in the tracking issue.
- [ ] **Restore drill green** within the last release cycle
      ([`backup-restore.md`](backup-restore.md) — `scripts/dev/restore-drill.sh`
      exit 0). A stale drill is no-go #1.
- [ ] Off-host copy confirmed (a backup on the same host as the DB is not a
      backup — see [`backup-restore.md` "Prod vs local"](backup-restore.md#prod-vs-local)).

### 1d. Secrets rotation confirmation

Confirm no secret rotation is mid-flight and the age key + DB creds are current
(referenced from [`incident-runbooks.md` §6](incident-runbooks.md#6-secret-rotation)).

- [ ] `scripts/deploy/render-config.sh --check` green against the prod SOPS
      bundle (no missing/blank required env vars).
- [ ] Age private key accessible to the deploy runner; no scheduled rotation
      falls inside the T-2 → T+24h deploy-freeze window.
- [ ] If a rotation is due, complete it at T-7 (not later) — see
      [`secrets-runbook.md` §"Rotate the age key"](../development/secrets-runbook.md#rotate-the-age-key-compromise--scheduled).

### 1e. Deploy freeze

From **T-2** through **T+24h**, no non-emergency promotions to prod. The on-call
is the only approver of an emergency promotion, and only via the rollback-ready
path in [`deploy.md`](deploy.md).

## 2. T-0 — event day (watch + respond)

### 2a. Pre-event final checklist (T-0 minus 30 min)

- [ ] **Open the on-call screen:** Grafana → `event-day-ops` board
      ([`dashboards/event-day-ops.json`](dashboards/event-day-ops.json)), last
      15 min window. Pin it.
- [ ] **Confirm the triage view is one click away:** `incident-triage` board
      ([`dashboards/incident-triage.json`](dashboards/incident-triage.json)).
- [ ] **Baseline the headline SLO strip** while traffic is low — note the green
      values so a drift is obvious. WS connect p95, broadcast p95,
      `flock_ws_receive_errors`, sessions %.
- [ ] **Confirm the rollback target** (§1c archive) + `FLOCK_PREVIOUS_TAG` are
      at hand.
- [ ] **Comms channels open:** `#ops` (the single alert channel per the
      [metrics design §3.2](metrics-alerting-design.md#32-paging-policy)) +
      the event owner DM.

### 2b. During-event — the watch posture

Watch the `event-day-ops` headline strip. The discipline:

1. **Read top-to-bottom:** WS connect p95 → broadcast p95 → `flock_ws_receive_errors`
   → sessions % → adapter Redis → DB pressure. The **first-mover tier** localizes
   the problem.
2. **Amber on 2+ SLOs with no single first-mover** = capacity exhaustion — go to
   [`incident-runbooks.md` §4](incident-runbooks.md#4-15k-event-degradation).
3. **Any critical page** → open the `incident-triage` view first, then follow the
   alert's deep link into [`incident-runbooks.md`](incident-runbooks.md).
4. **Post in `#ops`** that you have the page (single-channel principle).

### 2c. The pause lever (stabilization, not stop)

When an SLO is breaching and the tier needs relief, **pause new joins** rather
than restart the tier (a restart triggers a reconnect burst that worsens
overload — see [`incident-runbooks.md` §4b](incident-runbooks.md#4b-immediate-actions-stabilize--load-shed)):

- Tighten the edge rate limit on registration + realtime-connect
  ([FLO-294](/FLO/issues/FLO-294) / [FLO-319](/FLO/issues/FLO-319)) to cap *new*
  join rate below the provisioned connect budget. Existing attendees keep their
  sessions.
- The business "pause the event" call is the event owner's; the technical pause
  (rate-limit tighten) is the on-call's first stabilization move.

## 3. During-event common issues → escalation

The alert-routing table in [metrics design §3.4](metrics-alerting-design.md#34-alert-routing-table-critical-rows-only--see-1-for-the-full-threshold-set)
maps each critical alert to the response section. Reproduced here as the
fast-path table:

| Symptom | Incident | Response section |
| --- | --- | --- |
| WS connect/broadcast p95 > 1 s, receive errors > 0, sessions < 99% | WS connection storm | [`incident-runbooks.md` §1](incident-runbooks.md#1-ws-connection-storm) |
| Adapter Redis near-max / evicting / subscribers lost | Adapter Redis failover | [`incident-runbooks.md` §2](incident-runbooks.md#2-adapter-redis-failover) |
| Deadlocks, lock-waits, connection exhaustion | MariaDB deadlock / lock storm | [`incident-runbooks.md` §3](incident-runbooks.md#3-mariadb-deadlock--lock-storm) |
| Multiple SLOs amber, no single first-mover | 15k-event degradation | [`incident-runbooks.md` §4](incident-runbooks.md#4-15k-event-degradation) |
| Incident starts ≤ 30 min after a promotion | Deploy rollback | [`incident-runbooks.md` §5](incident-runbooks.md#5-deploy-rollback) → [`rollback.md`](rollback.md) |
| Suspected/confirmed secret compromise | Secret rotation | [`incident-runbooks.md` §6](incident-runbooks.md#6-secret-rotation) |

For severity, paging, and the incident-commander role, follow
[`incident.md`](incident.md).

## 4. T+24h — teardown checklist

- [ ] **Event traffic returned to baseline** on the on-call screen.
- [ ] **Re-arm normalcy:** relax the edge rate limit back to the steady-state
      cap (undo any §2c tightening); resume the RQ `long` queue if it was
      suspended (`rq resume`).
- [ ] **Lift the deploy freeze** (T+24h).
- [ ] **Confirm all alerts green** for the full event window; archive the
      `event-day-ops` board snapshot to the tracking issue.
- [ ] **No incidents outstanding** — every page acked + resolved, or handed to
      the [`incident.md`](incident.md) postmortem path.

## 5. Post-event

### 5a. Teardown + capacity follow-up

- Compare **actual peak concurrency** vs the provisioned budget. If the event
  over-sold vs the 15k load-proven budget, file a capacity follow-up
  ([FLO-245](/FLO/issues/FLO-245) topology) for the next event.
- If the §2c pause lever was used, record why and whether the cap needs a
  permanent adjustment.
- Prune event-day artifacts (old baseline backups per the
  [`backup-restore.md`](backup-restore.md) retention policy).

### 5b. Retrospective (Phase 6 acceptance)

Referenced from [`incident-runbooks.md` §4d/§7](incident-runbooks.md#4d-root-cause-post-event)
as the retrospective input for any in-event incident. Run within 1 business day:

- **Record the actuals:** peak concurrency, SLO breach pattern (which tier moved
  first, how long), provisioned tier vs the
  [hosting-quote](/FLO/issues/FLO-231#document-hosting-quote) envelope.
- **Per-incident:** detection-to-ack, detection-to-resolution, root cause,
  stabilization action, any pause/rollback decision. Link each to its
  [`incident.md`](incident.md) postmortem.
- **Decide code vs capacity:** 15k-event degradation is almost always capacity
  (a larger adapter Redis, more socketio workers, a bigger DB pool), not code.
  File PERF follow-ups for genuine query-plan regressions; do not hold the next
  event for a rewrite.
- **Feed the gate:** update [`launch-go-no-go.md`](launch-go-no-go.md) signals
  and [`metrics-alerting-design.md`](metrics-alerting-design.md) thresholds if
  the event revealed a threshold that was too loose/tight. The detection/
  response/timeline split means these edit independently.

## 6. Out of scope

- **Alert thresholds + paging policy** — owned by the
  [metrics design §3](metrics-alerting-design.md#3-alerting--thresholds-paging-escalation).
- **Per-incident technical response** — owned by
  [`incident-runbooks.md`](incident-runbooks.md).
- **Incident command (severity, comms templates, postmortem)** — owned by
  [`incident.md`](incident.md).
- **Backup/restore mechanics** — owned by [`backup-restore.md`](backup-restore.md).
  Referenced here as the rollback-target path (§1c).
- **Deploy + rollback mechanics** — owned by [`deploy.md`](deploy.md) /
  [`rollback.md`](rollback.md) (operator) and
  [`docs/development/deploy-runbook.md`](../development/deploy-runbook.md)
  (pipeline internals).

## 7. Related

- Parent strategy: [FLO-231](/FLO/issues/FLO-231) (Phase 6 — Production Launch).
- Parent epic: [FLO-533](/FLO/issues/FLO-533) (Phase 6.2 — observability, security & ops).
- Definition owner: [FLO-895](/FLO/issues/FLO-895) (operations runbooks).
- Cross-owner: [FLO-581](/FLO/issues/FLO-581) (event-day procedure layer).
- Detection (thresholds, paging): [`metrics-alerting-design.md`](metrics-alerting-design.md) ([FLO-586](/FLO/issues/FLO-586)).
- Per-incident response: [`incident-runbooks.md`](incident-runbooks.md) ([FLO-694](/FLO/issues/FLO-694)).
- Incident command: [`incident.md`](incident.md) ([FLO-895](/FLO/issues/FLO-895)).
- Deploy / rollback: [`deploy.md`](deploy.md) / [`rollback.md`](rollback.md) ([FLO-895](/FLO/issues/FLO-895)); pipeline internals [`docs/development/deploy-runbook.md`](../development/deploy-runbook.md) ([FLO-246](/FLO/issues/FLO-246)).
- Backup + restore (the rollback target): [`backup-restore.md`](backup-restore.md) ([FLO-288](/FLO/issues/FLO-288)).
- Launch go/no-go gate: [`launch-go-no-go.md`](launch-go-no-go.md) ([FLO-357](/FLO/issues/FLO-357)).
- Composite event-day load: [`scripts/dev/event_day.sh`](../../scripts/dev/event_day.sh) ([FLO-698](/FLO/issues/FLO-698)).
- Dashboards: [`dashboards/`](dashboards/) — `event-day-ops.json` (on-call screen), `incident-triage.json`.
