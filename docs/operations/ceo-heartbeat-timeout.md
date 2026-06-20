# CEO Heartbeat Timeout Enforcement Runbook

Owner: DevOps Engineer · Related: [CEO Silent Run Analysis](../architecture/ceo-silent-run-analysis.md) · Mechanism issue: [FLO-265](/FLO/issues/FLO-265)

This runbook defines the **timeout policy** that caps how long a single CEO
heartbeat run may stay active, and the **watchdog** that enforces it. It is the
platform-level bound referenced by the [monitoring](ceo-heartbeat-monitoring.md)
(FLO-266) and [recovery](ceo-recovery-runbook.md) (FLO-267) runbooks. It is
self-contained and works independently of those layers.

## Why this exists

The CEO is the top of the chain of command: every other agent reports (in)directly
to the CEO, so a stuck CEO is a single point of failure for the whole company.
The [2026-06-20 silent-run incident](../architecture/ceo-silent-run-analysis.md)
showed a CEO run holding its execution slot for >1 hour with **no timeout**. The
Paperclip control plane does not expose a native per-run timeout for the
`opencode_local` adapter, so this layer supplies one. It is recommendation #1 of
the incident analysis.

## Architecture (defense in depth)

```
                ┌──────────────────────────────────────────────┐
  CEO heartbeat │  */15 cron  →  CEO run  (normal 5–10 min)    │
  routine       └──────────────────────────────────────────────┘
                              │ held environment lease
                              ▼
   ┌────────────────────────────────────────────────────────────┐
   │  CEO Heartbeat Watchdog  (owner: DevOps)                   │
   │  policy:  manifest company.heartbeatConfig.timeoutSeconds  │
   │                                                            │
   │   detect open lease  →  age ≥ timeout?  →  terminate       │
   │                                          + incident comment │
   └────────────────────────────────────────────────────────────┘
        │ complements
        ├──── FLO-266 monitoring  (staleness alerts / webhook)  ─┐
        └──── FLO-267 recovery  (detect → release → restart)     │
                                                                     │
   layered together: timeout caps duration; monitoring detects;     │
   recovery restarts. Each works alone and composes. ───────────────┘
```

| Layer | Issue | Owner | Role |
|-------|-------|-------|------|
| **Timeout enforcement (this runbook)** | [FLO-265](/FLO/issues/FLO-265) | DevOps | Caps a single run's max duration; terminates over-time runs |
| Monitoring & observability | [FLO-266](/FLO/issues/FLO-266) | DevOps | Emits staleness alerts; can fire the recovery webhook |
| Recovery | [FLO-267](/FLO/issues/FLO-267) | Software Architect | Detect → release → restart → escalate |

## Configuration

The policy is declared in [`.paperclip/manifest.json`](../../.paperclip/manifest.json)
under `company.heartbeatConfig`:

```json
{
  "company": {
    "heartbeat": "CEO @ */15 * * * * (coalesce_if_active, skip_missed)",
    "heartbeatConfig": {
      "agent": "ceo",
      "cron": "*/15 * * * *",
      "concurrencyPolicy": "coalesce_if_active",
      "catchUpPolicy": "skip_missed",
      "timeoutSeconds": 720,
      "timeoutAction": "terminate",
      "graceSeconds": 60,
      "watchdog": "scripts/dev/ceo-heartbeat-watchdog.py"
    }
  }
}
```

| Field | Default | Valid range | Meaning |
|-------|---------|-------------|---------|
| `timeoutSeconds` | `720` (12 min) | `60`–`3600` | Max active run duration before the watchdog acts. |
| `timeoutAction` | `terminate` | `terminate` | What to do at timeout (only `terminate` is implemented). |
| `graceSeconds` | `60` | `>= 0` | SIGTERM→SIGKILL grace window. |
| `agent` | `ceo` | non-empty | Role the heartbeat (and timeout) applies to. |

### Why 12 minutes

Inside the incident's 10–15 min target, above the 5–10 min expected run length,
and below the 15-min cadence — so a killed run frees its slot **before** the next
cycle fires (which would otherwise `coalesce_if_active` into the stuck run). Tune
per workload; an out-of-bounds value is rejected loudly (never silently coerced).

## Detection model

A CEO heartbeat **execution** is bracketed in the company activity log by
`environment.lease_acquired` … `environment.lease_released` events sharing a
`runId`. An **open lease** (acquired, never released) whose age passed the timeout
is a genuinely stuck CEO execution — exactly the shape of the 2026-06-20 silent
run (lease held 02:15 → 03:33).

The watchdog does **not** use routine-run statuses for detection: by the time the
CEO is executing, the routine run has already gone terminal (`issue_created`/
`coalesced`). Relying on those would false-positive on every heartbeat. Routine
statuses `issue_created`, `coalesced`, `completed`, … are treated as terminal.

## Watchdog usage

Script: [`scripts/dev/ceo-heartbeat-watchdog.py`](../../scripts/dev/ceo-heartbeat-watchdog.py)
(stdlib only — no `pip install`). Env required for live enforcement:

```bash
export PAPERCLIP_API_URL=…        # control-plane base URL
export PAPERCLIP_API_KEY=…        # bearer token (run JWT or agent key)
export PAPERCLIP_COMPANY_ID=5a79febc-9067-4107-83a6-58974ec14a84
# Optional:
export FLOCK_CEO_AGENT_ID=d572935c-f075-471f-aab8-0bd2d9a975ba   # else resolved by role
export FLOCK_CEO_TERMINATE_CMD='…'   # adapter-specific kill; {pid}/{agent}/{run} substituted
```

```bash
# One-shot check + enforce (the cron/launchd entry point):
python3 scripts/dev/ceo-heartbeat-watchdog.py

# Observe-only: report what WOULD happen, terminate nothing, comment nothing:
python3 scripts/dev/ceo-heartbeat-watchdog.py --dry-run

# Foreground daemon (when not run via launchd/cron):
python3 scripts/dev/ceo-heartbeat-watchdog.py --interval 60

# Pure-logic self-test (no API, no processes):
python3 scripts/dev/ceo-heartbeat-watchdog.py --self-test
```

### Termination strategy

1. If `FLOCK_CEO_TERMINATE_CMD` is set, run it with `{pid}` / `{agent}` / `{run}`
   substituted — wire this for adapter-specific kill paths (e.g. an HTTP terminate
   endpoint). It is honoured verbatim if `{pid}` is absent.
2. Otherwise match the oldest local `opencode run` process whose elapsed time is
   at least the run age, SIGTERM, then SIGKILL after `graceSeconds`.

The watchdog acts on **at most one** process per pass and always pairs a kill
with an **incident comment** on the run's linked issue (skipped in `--dry-run`).

## Scheduling the watchdog

Run the one-shot pass every minute from launchd (macOS) or cron (Linux) under the
operator account that owns the `opencode` processes:

```cron
# crontab -e  (every minute; observe-only first, then drop --dry-run)
* * * * * cd /Users/mac/opencode-workspace-default/flock-os \
          && /usr/bin/env python3 scripts/dev/ceo-heartbeat-watchdog.py \
          >> /tmp/ceo-heartbeat-watchdog.log 2>&1
```

For a launchd equivalent, mirror `scripts/dev/start-adapter-redis.sh`'s
`Label`/`ProgramArguments`/`StartInterval` shape (the existing launchd pattern).

## Verification

1. **Pure logic (ops suite):** run
   `pytest tools/ops/ceo_heartbeat/tests/test_watchdog.py` — pins manifest policy
   parsing, ISO-8601 age math, the active/timed-out predicates, terminal-status
   handling (`issue_created`), and open-lease aggregation.
2. **Script self-test:** `python3 scripts/dev/ceo-heartbeat-watchdog.py --self-test`
   — 26/26 pure cases, no API, no processes. Use this as the runbook smoke gate.
3. **Live observe-only:** `--dry-run` against the real control plane confirms
   agent/lease resolution and prints `ok: 0/N open CEO execution leases over …`
   when the CEO is healthy (no false positives).

### Safe drill (no harm to CEO)

Run `--dry-run` while the CEO is healthy — it must report `0/N` open leases over
the timeout and terminate nothing. To exercise the timed-out branch without
touching the CEO, lower `timeoutSeconds` in a local manifest copy and run
`--dry-run` against a long-lived test run (do not point production enforcement at
a sub-minute timeout).

## Change log

| Date | Change | Source |
|------|--------|--------|
| 2026-06-20 | Initial policy + watchdog; manifest `heartbeatConfig` added | [FLO-265](/FLO/issues/FLO-265) |
