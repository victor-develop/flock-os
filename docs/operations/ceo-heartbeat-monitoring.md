# CEO heartbeat monitoring + observability — runbook

> Monitoring + alerting for the CEO heartbeat, shipped under
> [FLO-266](/FLO/issues/FLO-266). This is the **detection + observability** half
> of the CEO silent-run defense-in-depth:
>
> - [FLO-265](/FLO/issues/FLO-265) — timeout enforcement (runs cannot exceed bounds)
> - **[FLO-266](/FLO/issues/FLO-266)** — this — monitoring, observability, alert routing
> - [FLO-267](/FLO/issues/FLO-267) — recovery (watchdog daemon, auto-restart)
>
> Incident of record: [FLO-264](/FLO/issues/FLO-264) — a CEO heartbeat run went
> silent for ~1 hour before detection. Analysis:
> `docs/architecture/ceo-silent-run-analysis.md`. **Target: detect silent runs
> within minutes, not hours.**

## What it watches

The CEO is the top of the chain of command; a stuck CEO is a single point of
failure for the whole company. The monitor reads three signals from the
Paperclip control plane (no bench, no Frappe site required):

1. **Liveness** — the CEO agent's `status`, `lastHeartbeatAt`, and
   `orgChainHealth`. A CEO that has not heartbeated within the silence budget is
   the first smoke signal.
2. **Silent / stuck run detection** — a routine run whose status is non-terminal
   (`issue_created`) with `completedAt=null`. Once its age passes the warning
   (15m) / critical (30m) thresholds it becomes a silent-run alert. This is
   exactly the FLO-264 shape.
3. **Completion-time trends** — p50 / p95 / max of completed-run durations, so a
   creeping slowdown is visible before it becomes a silent run.

The "current operation" surface (the linked issue of the most-recent active run)
answers **"what is the CEO doing right now"** without anyone having to dig.

## Run it

```bash
# One-shot scrape. Exit code = severity: 0 ok, 1 warning, 2 critical, 3 config.
scripts/dev/ceo-heartbeat-monitor.py

# Structured snapshot (log shipping / structured alerting).
scripts/dev/ceo-heartbeat-monitor.py --format json

# Prometheus exposition (dashboard scrape).
scripts/dev/ceo-heartbeat-monitor.py --format prometheus

# Scrape + open idempotent Paperclip alert issues for any silent runs.
scripts/dev/ceo-heartbeat-monitor.py --alert
```

Configuration is env-driven (see `.env.example`). `PAPERCLIP_API_URL`,
`PAPERCLIP_API_KEY`, `PAPERCLIP_COMPANY_ID` are auto-injected inside a Paperclip
run; for a standalone cron job, provision a long-lived key and store it outside
the repo. `FLO_CEO_AGENT_ID` is required; `FLO_CEO_ROUTINE_ID` is auto-discovered.

## Deploy as a scheduled check

Until [FLO-267](/FLO/issues/FLO-267) ships a always-on watchdog daemon, run the
monitor on a 5-minute schedule. Two options:

**launchd (this Mac):**

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.flock-os.ceo-heartbeat-monitor</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/mac/opencode-workspace-default/flock-os/scripts/dev/ceo-heartbeat-monitor.py</string>
    <string>--alert</string>
  </array>
  <key>StartInterval</key><integer>300</integer>
  <key>EnvironmentVariables</key>
  <dict><!-- PAPERCLIP_*, FLO_CEO_* from a secrets store — never the repo --></dict>
  <key>StandardOutPath</key><string>/tmp/ceo-heartbeat-monitor.log</string>
  <key>StandardErrorPath</key><string>/tmp/ceo-heartbeat-monitor.log</string>
</dict>
</plist>
```

**cron:** `*/5 * * * * cd /path/to/flock-os && ./scripts/dev/ceo-heartbeat-monitor.py --alert >> /tmp/ceo-heartbeat-monitor.log 2>&1`

A dedicated Paperclip **routine** is the natural long-term home once the
monitor is blessed — a routine fires this script as its run issue and surfaces
its own silent-run the same way the CEO's does.

## Alert routing

`--alert` opens one Paperclip issue per silent run that lacks an existing **open**
alert (idempotent — dedupes by searching issues for the run id, so a 5-minute
cadence never stacks duplicates). Routing follows Rule #1 (**never page a human
for what an agent could do**):

- **Default assignee**: the Software Architect (role `cto`) — the CEO's de-facto
  recovery owner (handled FLO-264; owns CEO recovery until FLO-267 ships
  auto-restart). Override with `FLO_CEO_MONITOR_ESCALATION_AGENT_ID`.
- **Priority**: `critical` for 30m+ silent runs, `high` for 15m+ warning runs.
- **Parent**: `FLO_CEO_HEARTBEAT_ISSUE_ID` (FLO-263) so alerts thread under the
  routine.

Alerts are `blocked` on creation with the silent run id + linked issue in the
title/description, pointing the recovery owner at the exact stuck run.

## Exit codes (for wrappers)

| Code | Meaning    | Action                                                                   |
| ---- | ---------- | ------------------------------------------------------------------------ |
| 0    | OK         | CEO healthy, no silent runs.                                             |
| 1    | WARNING    | A run crossed 15m silence, or `lastHeartbeatAt` is stale past 15m.       |
| 2    | CRITICAL   | A run crossed 30m silence, or org-chain health is `unhealthy`.           |
| 3    | CONFIG     | Missing credentials / network failure. **Not** a CEO health signal — fix the monitor config, do not page the CEO owner. |

## Manual recovery (until FLO-267)

When the monitor pages a silent run:

1. Read the alert issue — it names the run id, age, and the linked issue the CEO
   was working (`current operation`).
2. `GET /api/issues/{linkedIssueId}` to see what the CEO was doing.
3. If the run is genuinely stuck (not stale state), the recovery actions in
   `docs/architecture/ceo-silent-run-analysis.md` §"Immediate Actions" apply —
   terminate the stuck run, re-trigger via
   `POST /api/agents/{ceoId}/heartbeat/invoke` or `POST /api/routines/{id}/run`.
4. File the recovery under the alert issue so the timeline is preserved.

> Stale-state note: a run may remain in `issue_created` with `completedAt=null`
> after the underlying problem was resolved (Paperclip's own stale-detection did
> not close it — the FLO-264 incident left exactly such a residue). The monitor
> reports the signal faithfully; if a silent run is confirmed stale, close its
> linked issue so the monitor stops alerting on it.

## Architecture (ports & adapters)

Mirrors `flock_os/telemetry.py` (the Frappe-runtime observability twin):

```
PaperclipCEOHeartbeatSource  (port)      <- production: Paperclip REST API (GET only)
StaticCEOHeartbeatSource                   <- in-memory, for the unit suite
PaperclipAlertIssuer                       <- production: Paperclip REST API (POST)
    -> detect_silent_runs()                <- pure, threshold logic (15m/30m)
    -> compute_completion_stats()          <- pure, p50/p95/max duration trends
    -> evaluate_health()                   <- pure, the CEOHealthSnapshot
    -> snapshot_as_{text,json,prometheus}  <- dashboard / scrape surfaces
CEOHeartbeatMonitor                        <- orchestrates one scrape
```

The pure detection math is fully unit-tested without a network
(`flock_os/tests/test_ceo_heartbeat_monitor.py`); the Paperclip adapters take
injectable HTTP fetchers so the suite drives them against canned responses. The
HTTP layer defaults to `urllib` (no third-party dependency) and maps every error
to `RuntimeError` so a scrape never silently degrades.

## Dashboard metrics (Prometheus)

| Metric                                  | Type      | Meaning                                          |
| --------------------------------------- | --------- | ------------------------------------------------ |
| `ceo_health_severity`                   | gauge     | 0 ok / 1 warning / 2 critical.                   |
| `ceo_last_heartbeat_timestamp_seconds`  | gauge     | Epoch of the CEO's last heartbeat.               |
| `ceo_active_run_count`                  | gauge     | In-flight runs right now.                        |
| `ceo_silent_run_alert_count`            | gauge     | Silent runs over threshold.                      |
| `ceo_silent_run_age_minutes`            | gauge     | One series per silent run (labels: severity, run_id, linked_issue). |
| `ceo_heartbeat_completion_seconds`      | summary   | Completed-run duration p50/p95/max + count/sum.  |

## Related

- Source: `flock_os/ceo_heartbeat_monitor.py` · CLI: `scripts/dev/ceo-heartbeat-monitor.py`
- Tests: `flock_os/tests/test_ceo_heartbeat_monitor.py` (49 tests, 91% module coverage)
- Incident analysis: `docs/architecture/ceo-silent-run-analysis.md`
- Siblings: [FLO-265](/FLO/issues/FLO-265) timeout · [FLO-267](/FLO/issues/FLO-267) recovery
