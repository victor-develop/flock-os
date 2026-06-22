# Incident response runbook — Flock OS (FLO-895 — Phase 6.2)

> **Definition owner:** [FLO-895](/FLO/issues/FLO-895) (Phase 6.2 — operations
> runbooks). **Parent epic:** [FLO-533](/FLO/issues/FLO-533) (Phase 6.2 —
> observability, security & ops). **Strategy:** [FLO-231](/FLO/issues/FLO-231)
> (Phase 6 — Production Launch).
>
> This is the **command layer** — how an incident is declared, paged,
> communicated, and postmortemed. It owns severity levels, the paging chain,
> and the comms/postmortem templates. The per-incident **technical response**
> (which commands to run for a WS storm, a Redis failover, a DB deadlock) lives
> in the sibling [`incident-runbooks.md`](incident-runbooks.md)
> ([FLO-694](/FLO/issues/FLO-694)); the **detection** (thresholds, paging
> routing) lives in the [metrics design §3](metrics-alerting-design.md#3-alerting--thresholds-paging-escalation)
> ([FLO-586](/FLO/issues/FLO-586)). The event-day **timeline** (when the on-call
> is on watch) lives in [`event-day-runbook.md`](event-day-runbook.md)
> ([FLO-581](/FLO/issues/FLO-581)).
>
> **VM-independent:** severities, paging, and templates need no staging VM. The
> exact pager IDs fill in when the prod VM is up ([FLO-249](/FLO/issues/FLO-249)).

## TL;DR

- **Declare the severity first.** Sev1 → page the on-call + the incident
  commander now; Sev2 → page the on-call; Sev3/Sev4 → `#ops` + tracking issue.
  Severity drives paging, not the other way around.
- **Ack < 1 min in an event window** ([metrics design §3.2](metrics-alerting-design.md#32-paging-policy)).
  Acking stops the escalation timer; it does not mean the incident is over.
- **Open the [`incident-triage`](dashboards/incident-triage.json) view before
  typing commands.** The correlated tier view tells you where to look — see
  [`incident-runbooks.md` §0](incident-runbooks.md#0-universal-triage--open-this-first).
- **Communicate on a cadence.** First update within 15 min of ack; updates at
  least every 30 min until resolved. Use the templates in §4.
- **Every Sev1/Sev2 gets a postmortem** (§5) within 2 business days. No blame;
  mechanism + guardrail.

## 1. Severity levels

Severity is declared by the incident commander (the on-call, by default) based
on **customer impact + scope**, not on which metric fired. The mapping to the
detection layer's critical/warning/info is in §6.

| Severity | Definition | Examples | Response |
|----------|------------|----------|----------|
| **Sev1 — Critical** | A core flow is down or data is at risk for live users. The 15k-event SLO will not hold without intervention. | WS tier down; adapter Redis evicting (broadcasts dropping); bulk-attendance path deadlocking; site-wide 5xx; suspected secret compromise | Page on-call + IC now; ack < 1 min (event) / < 5 min (off-event); resolve < 5 min (event) / < 30 min (off-event) |
| **Sev2 — High** | A core flow is degraded but not down; a non-SLO regression is observed that will become Sev1 if unaddressed. | WS connect p95 trending to 1 s but under; DB lock-waits climbing; single socketio worker down (tier still serving) | Page on-call; ack < 5 min; resolve < 30 min |
| **Sev3 — Medium** | A non-core flow is degraded, or a warning metric with no user impact yet. | Restore drill stale; queue depth warning; a background job failing | `#ops` + tracking issue; triage < 4 business hours |
| **Sev4 — Low** | Cosmetic / info; no user impact. | Baseline drift; doc typo in a runbook | Metrics log / tracking issue; no notification |

> **The SLO floor.** Per the [metrics design §3.1 severity rubric](metrics-alerting-design.md#31-severity-rubric),
> a §8 WS SLO violation, adapter-Redis exhaustion, DB connection exhaustion, or
> non-zero `flock_ws_receive_errors` is always at least Sev1 — the 15k-event SLO
> is the launch gate ([no-go #9](launch-go-no-go.md#no-go-conditions)).

## 2. Who to page

The paging chain. Pager IDs fill in when the prod schedule is live; until then
these are **roles**, recorded per-event in the
[`event-day-runbook.md` §0 roster](event-day-runbook.md#0-roles--on-call-roster).

| Step | Sev1 | Sev2 | Sev3/4 |
|------|------|------|--------|
| 1 | **On-call engineer** — PagerDuty phone+SMS+push (event) / push (off-event) | On-call — PagerDuty push | `#ops` post only |
| 2 (no ack in SLA) | Escalate to **incident commander** (second engineer) | Escalate to on-call primary | — |
| 3 (no response) | Escalate to **DevOps lead** → **Architect** | Escalate to DevOps lead | Tracking issue assigned |
| 4 (business impact) | **CEO / event owner** — the business "pause the event" call (see [`event-day-runbook.md` §2c](event-day-runbook.md#2c-the-pause-lever-stabilization-not-stop)) | Notify event owner in `#ops` | — |

> **Single-channel principle** ([metrics design §3.2](metrics-alerting-design.md#32-paging-policy)):
> every alert — page or no page — also posts to `#ops` so the warning→critical
> promotion is visible in thread context. Pages layer on top; they do not
> bypass the channel.

### Secret compromise — page before you investigate

If a secret (age private key, DB root password, admin password) is suspected
compromised, **rotate before investigating** — see
[`incident-runbooks.md` §6](incident-runbooks.md#6-secret-rotation) and the
[pre-production audit](../security/pre-production-audit.md) zero-hardcoded-secrets
posture. This is a Sev1 regardless of visible impact.

## 3. Incident lifecycle

Every incident, regardless of severity, moves through the same lifecycle. The
**technical steps** at each stage are in [`incident-runbooks.md`](incident-runbooks.md);
this section is the commander's flow.

1. **Detect** — a critical alert fires (paged) or a warning is spotted on the
   [`event-day-ops`](dashboards/event-day-ops.json) board. The alert's
   notification body includes a deep link to the matching response section —
   see [metrics design §3.3](metrics-alerting-design.md#33-escalation-to-the-event-day-runbook).
2. **Ack** — within the paging SLA (§1/§2). Post in `#ops` that you have the
   page. Declaring the severity (§1) happens here.
3. **Triage** — open the [`incident-triage`](dashboards/incident-triage.json)
   view. Read the correlated tier view top-to-bottom: which tier moved first?
   Did it start within ~30 min of a promotion (→ deploy rollback)? Match the
   firing alert(s) to the
   [routing table](incident-runbooks.md#0-universal-triage--open-this-first)
   and jump to that section.
4. **Stabilize** — run the matched response section's immediate actions. Prefer
   load-shedding (the [`event-day-runbook.md` §2c](event-day-runbook.md#2c-the-pause-lever-stabilization-not-stop)
   pause lever) over restarts mid-event. Decide pause/rollback per the section's
   "When to pause/rollback" line.
5. **Resolve** — the firing alert returns to green; the SLO holds; the affected
   flow is confirmed working. Re-arm the alert before closing.
6. **Postmortem** (§5) — Sev1/Sev2 within 2 business days. File the incident
   record (the tracking issue) per [`incident-runbooks.md` §7](incident-runbooks.md#7-post-incident-every-incident).

## 4. Communication templates

Copy-paste starting points. Fill the `<…>` slots. Post Sev1/Sev2 updates to
`#ops` on a cadence (first within 15 min of ack, then ≥ every 30 min until
resolved). Customer-facing comms are the event owner's call.

### 4a. Initial page / declaration (in `#ops`, within ack SLA)

```
🚨 [SEV<n>] declared — <one-line symptom>
IC: <on-call name> | Started: <HH:MM TZ>
Triage: opened incident-triage view; first-mover tier = <WS/Redis/DB/Edge/multi>
Response: following <incident-runbooks §X> — <stabilization action underway>
Next update: <time, ≤ 30 min>
```

### 4b. Status update (every ≤ 30 min while open)

```
Update [<SEV n>] <HH:MM> — still <investigating/stabilizing/monitoring>
Current: <what's happening now, e.g. "WS connect p95 1.4s, tightening edge cap">
Action taken: <pause lever / rollback / restart — which section>
Impact: <who is affected, e.g. "new joins queued; existing sessions OK">
Next update: <time, ≤ 30 min> | Need: <any blocker / who you need>
```

### 4c. Resolution (on close)

```
✅ [<SEV n>] resolved — <HH:MM TZ> (duration: <Xm>)
Root cause: <one line> | Stabilization: <one line>
Rollback/pause used: <yes/no — what> | Data impact: <none / described>
Alert re-armed: <yes> | Postmortem: <tracking issue link, due <date>>
```

### 4d. Customer / attendee-facing (event owner owns this)

```
Subject: <Service> is <degraded/recovered>
We're seeing <symptom in plain language>. <Existing attendees keep their sessions /
New check-ins are briefly delayed.> We're on it and will update here by <time>.
— Update <n>: Resolved as of <HH:MM>. Thank you for your patience.
```

## 5. Postmortem template

File as an issue (the incident record) linked from the event tracking issue.
**Blameless** — focus on mechanism and guardrail, not individual. Due within
**2 business days** for Sev1/Sev2.

```markdown
# Postmortem — <incident title> (<SEV n>, <date>)

## Summary
One paragraph: what happened, impact, duration.

## Timeline (all times TZ)
- <HH:MM> — alert fired (<alert name>); paged on-call.
- <HH:MM> — acked; triage view opened; first-mover = <tier>.
- <HH:MM> — <stabilization action, e.g. pause lever tightened>.
- <HH:MM> — SLO recovered; alert green.
- <HH:MM> — resolved.

## Impact
- Users affected: <count / %>.
- Data impact: <none / described, e.g. "broadcasts dropped for ~90s">.
- SLO breach: <which signal S4–S7, by how much, for how long>.

## Root cause
The mechanism, not the person. <What condition allowed this — config drift,
capacity shortfall, query-plan regression, missing guardrail.>

## What worked
- <e.g. "the correlated triage view localized the first-mover in <60s".>

## What didn't
- <e.g. "the edge rate-limit cap required a code change instead of a config edit".>

## Action items (owner, issue link, due)
- [ ] <action> — @owner — [FLO-XXX](/FLO/issues/FLO-XXX)
- [ ] <threshold tuning in metrics design §X> — @owner
- [ ] <runbook/procedure edit — which doc/section> — @owner
```

> **Feed the docs.** If the incident revealed a threshold that was too
> loose/tight, update the [metrics design §3](metrics-alerting-design.md#3-alerting--thresholds-paging-escalation).
> If a response step didn't work, update [`incident-runbooks.md`](incident-runbooks.md).
> If a timeline step was missing, update [`event-day-runbook.md`](event-day-runbook.md).
> The command/detection/response/timeline split means these edit independently.

## 6. Severity × detection mapping

How the [metrics design §3.1](metrics-alerting-design.md#31-severity-rubric)
detection tiers map to incident severity:

| Detection | Incident severity | Response path |
|-----------|-------------------|---------------|
| Critical (§8 WS SLO breach / adapter exhaustion / DB exhaustion / receive errors) | Sev1 | Page + [`incident-runbooks.md`](incident-runbooks.md) §1–§4 |
| Critical ≤ 30 min after a promotion | Sev1 | [`incident-runbooks.md` §5](incident-runbooks.md#5-deploy-rollback) → [`rollback.md`](rollback.md) |
| Suspected/confirmed secret compromise | Sev1 | [`incident-runbooks.md` §6](incident-runbooks.md#6-secret-rotation) — rotate first |
| Warning (trending to critical, non-SLO regression) | Sev2/Sev3 | `#ops` + tracking issue; triage per [`incident-runbooks.md`](incident-runbooks.md) |
| Info (baseline drift, no SLO impact) | Sev4 | Metrics log; retrospective input |

## 7. Out of scope

- **Per-incident technical response procedures** — owned by
  [`incident-runbooks.md`](incident-runbooks.md).
- **Alert thresholds + paging routing** — owned by the
  [metrics design §3](metrics-alerting-design.md#3-alerting--thresholds-paging-escalation).
- **The event-day timeline (T-7 → T+24h)** — owned by
  [`event-day-runbook.md`](event-day-runbook.md).
- **Rollback mechanics** — owned by [`rollback.md`](rollback.md) +
  [`docs/development/deploy-runbook.md`](../development/deploy-runbook.md).
- **The security posture itself** — owned by
  [`docs/security/pre-production-audit.md`](../security/pre-production-audit.md).

## 8. Related

- Parent strategy: [FLO-231](/FLO/issues/FLO-231) (Phase 6 — Production Launch).
- Parent epic: [FLO-533](/FLO/issues/FLO-533) (Phase 6.2 — observability, security & ops).
- Definition owner: [FLO-895](/FLO/issues/FLO-895) (operations runbooks).
- Detection (thresholds, paging): [`metrics-alerting-design.md`](metrics-alerting-design.md) ([FLO-586](/FLO/issues/FLO-586)).
- Per-incident response: [`incident-runbooks.md`](incident-runbooks.md) ([FLO-694](/FLO/issues/FLO-694)).
- Event-day timeline: [`event-day-runbook.md`](event-day-runbook.md) ([FLO-581](/FLO/issues/FLO-581)).
- Rollback: [`rollback.md`](rollback.md) / [`docs/development/deploy-runbook.md`](../development/deploy-runbook.md) ([FLO-246](/FLO/issues/FLO-246)).
- Backup + restore: [`backup-restore.md`](backup-restore.md) ([FLO-288](/FLO/issues/FLO-288)).
- Security posture: [`docs/security/pre-production-audit.md`](../security/pre-production-audit.md).
- Dashboards: [`dashboards/`](dashboards/) — `incident-triage.json`, `event-day-ops.json`.
