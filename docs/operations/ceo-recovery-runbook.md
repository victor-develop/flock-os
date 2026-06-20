# CEO Agent Recovery Runbook

Owner: Software Architect · Related: [CEO Silent Run Analysis](../architecture/ceo-silent-run-analysis.md) · Mechanism issue: [FLO-267](/FLO/issues/FLO-267)

This runbook defines the **automatic** recovery path for a stuck CEO heartbeat and
the **manual** override a board operator uses when automation cannot resolve it. It
is the deterministic program the **CEO Liveness Watchdog** routine executes on every
fire, and the reference a human follows during an incident.

## Why this exists

The CEO is the top of the chain of command. When a CEO heartbeat run goes silent,
`coalesce_if_active` causes every subsequent scheduled heartbeat to coalesce into
the stuck run, so the CEO produces no new work and there is no higher authority to
intervene. The [2026-06-20 incident](../architecture/ceo-silent-run-analysis.md)
showed five consecutive heartbeats (02:15–03:30 UTC) coalescing into one silent run
before anyone noticed.

The watchdog closes that gap: a **peer agent** (Software Architect, whose heartbeats
are independent of the CEO's) detects staleness and restarts the CEO automatically.

## Architecture (defense in depth)

```
                ┌──────────────────────────────────────────────┐
  CEO heartbeat │  */15 cron  →  CEO run  (normal 5–10 min)    │
  routine       └──────────────────────────────────────────────┘
                              │ liveness signal
                              ▼
   ┌────────────────────────────────────────────────────────────┐
   │  CEO Liveness Watchdog  (owner: Software Architect)         │
   │  triggers: schedule (backstop) + webhook (event path)       │
   │                                                             │
   │   detect  →  terminate(stuck slot)  →  restart(backoff ×3)  │
   │                                          │ fail ×3           │
   │                                          ▼                   │
   │                                  escalate to board           │
   └────────────────────────────────────────────────────────────┘
        ▲                                  │ integrates when ready
        │ event path (webhook)             │
        └──────── FLO-266 monitoring ──────┘   (DevOps)
   FLO-265 timeout enforcement caps run duration at the platform level
   (independent of this runbook; listed here for completeness).
```

| Layer | Issue | Owner | Role |
|-------|-------|-------|------|
| Timeout enforcement | [FLO-265](/FLO/issues/FLO-265) | DevOps | Caps a single run's max duration (platform level) |
| Monitoring & observability | [FLO-266](/FLO/issues/FLO-266) | DevOps | Emits staleness alerts; can fire the watchdog webhook |
| **Recovery (this runbook)** | [FLO-267](/FLO/issues/FLO-267) | Software Architect | Detect → terminate → restart → escalate |

This runbook is **self-contained**: it performs its own detection and recovery so it
works even before FLO-265/FLO-266 land. When they do, the watchdog gains an
event-driven webhook trigger (no idle polling cost) and respects the platform timeout.

## Constants

| Constant | Value | Rationale |
|----------|-------|-----------|
| `CEO_AGENT_ID` | `d572935c-f075-471f-aab8-0bd2d9a975ba` | CEO agent |
| `CEO_ROUTINE_ID` | `f2f8f757-b16e-425a-8fda-fe085d2da2e5` | CEO heartbeat routine |
| `WATCHDOG_OWNER` | `f893dff0-4d7c-4d24-9234-a8d5de384fa1` | Software Architect |
| `T_SUSPICIOUS` | 15 min | Exceeds a normal 5–10 min heartbeat + 1 cycle |
| `T_STUCK` | 20 min | Detect threshold — recovery starts here |
| `T_ESCALATE` | 30 min | Hard threshold — board must be notified |
| `T_ABORT` | 45 min | Stop automated retry; human-only from here |
| `MAX_ATTEMPTS` | 3 | Restart attempts per recovery action |
| Backoff | 30 s → 60 s → 120 s | Exponential delays between attempts |

## Automatic recovery — what the watchdog does each fire

The watchdog is the Software Architect waking on a **CEO Liveness Watchdog** run
issue. On every fire it runs the procedure below and records the outcome as a
comment on the run issue (and on [FLO-263](/FLO/issues/FLO-263) only when it acts).

### Step 1 — Detect (read liveness)

```bash
GET /api/routines/{CEO_ROUTINE_ID}/runs?limit=20
```

Find the **base run**: the most recent run whose `status` is **not** `coalesced`
(its `coalescedIntoRunId` is null). That is the run currently holding the CEO slot.
Let `linkedIssueId` = base run's `linkedIssueId`, `startedAt` = base run's
`triggeredAt`, and `age` = now − `startedAt`.

Fetch the linked issue to read its real status:

```bash
GET /api/issues/{linkedIssueId}
```

**Verdict:**
- If the linked issue is terminal (`done`/`cancelled`) → CEO is **healthy**. Exit.
- If `age < T_SUSPICIOUS` → **healthy** (run is within normal bounds). Exit.
- If `T_SUSPICIOUS ≤ age < T_STUCK` → **suspicious**. Log a watch comment. Exit.
- If `age ≥ T_STUCK` and the linked issue is still active (`in_progress`) → **STUCK**. Proceed to Step 2.

> **Composability:** when FLO-266 lands, prefer its monitoring signal (or the
> webhook trigger) as the detection source and use the run-history check above as
> the fallback only.

### Step 2 — Terminate (free the stuck slot)

Release the stuck issue so the next heartbeat is not coalesced away:

```bash
POST /api/issues/{linkedIssueId}/release
{ "agentId": "{WATCHDOG_OWNER}" }
Headers: Authorization: Bearer $PAPERCLIP_API_KEY, X-Paperclip-Run-Id: $PAPERCLIP_RUN_ID
```

> **Scope note.** Releasing frees the issue checkout/slot so a fresh heartbeat can
> be admitted. It does **not** kill the underlying adapter process — capping the
> process duration is FLO-265's job (platform timeout). If `release` returns
> `401/403` (the watchdog lacks authority over the CEO's checkout), skip the retry
> loop and go straight to **Step 4 — Escalate**.

### Step 3 — Restart (auto-restart with exponential backoff, max 3)

Kick a fresh CEO heartbeat, then re-check liveness. Repeat with backoff:

```bash
POST /api/agents/{CEO_AGENT_ID}/heartbeat/invoke
Headers: Authorization: Bearer $PAPERCLIP_API_KEY, X-Paperclip-Run-Id: $PAPERCLIP_RUN_ID
```

Pseudocode:

```
for attempt in 1..MAX_ATTEMPTS:
    if attempt > 1: sleep(backoff[attempt-1])        # 30s, 60s, 120s
    invoke CEO heartbeat
    sleep(45s)                                       # let it admit
    re-read base run + linked issue (Step 1)
    if linked issue terminal OR a NEW non-coalesced run appeared:
        mark RECOVERED on attempt N; break
if not recovered: go to Step 4
```

A recovery is **successful** when a fresh non-coalesced run appears and progresses
(new output / linked issue advances), or the prior linked issue moves to terminal.

Record a single comment on [FLO-263](/FLO/issues/FLO-263): *“Watchdog recovered CEO
run `<base run id>` after `<age>` min via release+invoke (attempt N).”*

### Step 4 — Escalate (automation exhausted)

Triggered when: 3 attempts fail, **or** `age ≥ T_ESCALATE` and Step 2 was denied,
**or** `age ≥ T_ABORT`. Stop retrying — do not hammer the CEO.

- Post an **incident comment** on [FLO-263](/FLO/issues/FLO-263) summarising
  detection time, base run id, attempts made, and the exact error (e.g. release
  `403`).
- Create a **board approval** (`POST /api/companies/{companyId}/approvals`,
  `type: request_board_approval`) requesting authorisation to force-release /
  pause-resume the CEO, linking `issueIds: ["<FLO-263 issue id>"]`.
- Do **not** auto-pause the CEO without board authorisation (pausing leadership has
  org-wide blast radius).

## Manual override (board / operator)

Use when automation is paused, has failed, or during a declared incident. All calls
need a board/operator API key and `X-Paperclip-Run-Id` is not required for manual ops.

### Fast path — force a fresh CEO heartbeat

```bash
POST /api/agents/d572935c-f075-471f-aab8-0bd2d9a975ba/heartbeat/invoke
```

### Free a stuck CEO checkout

```bash
POST /api/issues/{stuck-heartbeat-issue-id}/release
```

### Pause / resume the CEO (use only if restarts keep failing)

```bash
POST /api/agents/d572935c-f075-471f-aab8-0bd2d9a975ba/pause     # stops heartbeats
POST /api/agents/d572935c-f075-471f-aab8-0bd2d9a975ba/resume    # resumes
```

While paused, scheduled triggers coalesce/skip; resume admits the next cycle.

### Roll back a bad CEO config

If the stuck run followed a config change, roll it back:

```bash
GET    /api/agents/d572935c-f075-471f-aab8-0bd2d9a975ba/config-revisions
POST   /api/agents/d572935c-f075-471f-aab8-0bd2d9a975ba/config-revisions/{revisionId}/rollback
```

## Watchdog routine configuration

| Field | Value |
|-------|-------|
| Title | CEO Liveness Watchdog |
| Assignee | Software Architect (`f893dff0-4d7c-4d24-9234-a8d5de384fa1`) |
| Project | Flock OS Platform (`747bf0c1-8744-4bd8-b215-2d1456dc1236`) |
| Concurrency | `coalesce_if_active` (one watchdog run at a time) |
| Catch-up | `skip_missed` |
| Initial status | **`paused`** — activates on CEO/board approval (see [FLO-267](/FLO/issues/FLO-267)) |

**Triggers:**

1. **Schedule (backstop):** `10,25,40,55 * * * *` (UTC) — fires ~10 min after each
   CEO cycle so a normal heartbeat has finished and the liveness signal is clean.
   Cadence gives detection within ~10–20 min and recovery within ~5 min of detection.
2. **Webhook (event path):** firing URL is emitted when the trigger is created; wire
   [FLO-266](/FLO/issues/FLO-266) monitoring to POST here on a staleness alert for
   zero-idle, event-driven recovery.

> **Cadence vs budget.** A 15-min backstop cadence is justified while adapter spend
> is $0 (local model). If the company moves to a metered adapter, raise the cadence
> to 30 min (`10,40 * * * *`) and lean on the FLO-266 webhook as the primary path.

## Verification

The watchdog run is itself the verification loop: every fire either confirms healthy
(no-op comment) or performs measurable recovery. To exercise the path safely:

1. With the watchdog **paused**, manually fire it:
   `POST /api/routines/{watchdogRoutineId}/run` while the CEO is healthy — it should
   detect *healthy* and exit without acting.
2. To simulate a stuck slot without harming the CEO, create a throwaway `in_progress`
   issue, point a dry-run detection query at it, and confirm the verdict logic
   produces *STUCK* at the expected age. (Do not release real CEO issues in a drill.)

## Change log

| Date | Change | Source |
|------|--------|--------|
| 2026-06-20 | Initial runbook; watchdog created (paused) | [FLO-267](/FLO/issues/FLO-267) |
