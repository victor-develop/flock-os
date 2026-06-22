# Launch go/no-go readiness gate — Flock OS (FLO-357 / FLO-332 — Phase 6.2)

> **Definition owner:** [FLO-357](/FLO/issues/FLO-357) (no-board signal catalog +
> sign-off card slice). **Originated by:** [FLO-332](/FLO/issues/FLO-332)
> (VM-independent gate + migration runbook). This document is the canonical gate
> definition; both issues converge here.

> **Automated companion:** [`scripts/launch-gate.sh`](../../scripts/launch-gate.sh)
> ([FLO-354](/FLO/issues/FLO-354)) machine-checks every criterion below that has
> repo-local evidence (merge gate, artifact presence, coverage floor, migration
> debt, realtime tier) and prints the human/cloud/board items as reminders. This
> document is the human sign-off checklist; the script is its automation. **Green
> script + the four signatures below == GO.** Run `scripts/test_launch_gate_hygiene.sh`
> to regression-test the script's own invariants.

> The sign-off checklist **DevOps + QA + CEO** walk together before promoting the
> first **real** event to production. The **Signal catalog** (next section) lists
> every pre-launch signal with its **target** and **source script/run**; §1 is
> the issue-oriented prerequisite list; §2 is the merge-gate/coverage bar. If
> every box is checked, the launch is a **go**. If any **no-go condition**
> (§"No-go conditions") holds, the launch is a **no-go** — fix the item and
> re-walk this gate.
>
> **This gate definition is VM-independent and no-board** (drafted and
> CEO-reviewed with zero cloud spend, no approval required — see
> [FLO-357](/FLO/issues/FLO-357)), but **the launch itself is gated on real
> evidence**: green smokes, a green restore drill, the §8 scale SLOs reproduced
> at 15k, and the three signatures. Stubs/TODOs in the runbooks are no-go on
> their own.

## TL;DR — the decision

| Role | Signs off on | Gate |
|------|--------------|------|
| **DevOps** | Deploy pipeline green end-to-end, prod env reachable + healthy, rollback drill recorded | §1 |
| **QA** | Smoke gates green, coverage bar held, checklist matches the merge-gate reality | §1 + §2 |
| **CEO** | Launch partner named + onboarded, budget approved, business go | §3 |
| **Architect** | Topology + migration + realtime tier proven; this checklist is complete + accurate | (gate author) |

**A launch needs all four.** A missing signature is a no-go.

## Signal catalog — every pre-launch signal, target, source

> The single source of truth for **what is measured** before launch. Each signal
> names its **target** and the **source script/run** that produces the evidence.
> Consumes the [FLO-347](/FLO/issues/FLO-347) §8 scale targets and the
> [FLO-350](/FLO/issues/FLO-350) migration-drift + restore-drill outputs — it
> does **not** rebuild them. The [FLO-354](/FLO/issues/FLO-354)
> `scripts/launch-gate.sh` companion automates the rows that have repo-local
> evidence; the rest are produced at the go/no-go meeting from the cited run.

### Coverage & correctness (hold on every deploy)

| # | Signal | Target | Source script/run | Owner | Gate § |
|---|--------|--------|-------------------|-------|--------|
| S1 | Branch coverage at promoted tag | **≥ 91.29%** (current measured bar — do not regress) | [`scripts/qa-gate.sh`](../../scripts/qa-gate.sh) → `pytest flock_os/tests/` + `[tool.coverage.report]` in [`pyproject.toml`](../../pyproject.toml) (`fail_under = 80` is the ratchet floor, not the launch bar) | QA | §2 |
| S2 | Permission matrix | green — no SEC-1..SEC-6 regression across the role × DocType × position matrix | [`docs/security/permission-audit.md`](../security/permission-audit.md) matrix test ([FLO-290](/FLO/issues/FLO-290)) | Arch | §1b |
| S3 | Lint + format gate | green | `ruff check` → `ruff format --check` (via `scripts/qa-gate.sh` / `.github/workflows/ci.yml`) | QA | §2 |

### Scale — the §8 targets reproduced at 15k ([FLO-347](/FLO/issues/FLO-347))

> Run via `k6 run -e WS_VUS=15000 -e WS_DURATION_SEC=120 -e WS_BASE_URL=ws://<lb-host>:9000 load/ws_event_room.js` against the prod-shape nginx WS-upgrade LB tier (`scripts/dev/scale-socketio.sh --lb nginx`). The k6 thresholds in `load/ws_event_room.js` already enforce S4–S6 (`p(95)<1000`, `count==0`); the gate asserts the run **ended clean** (k6 exit 0) and the summary JSON is archived.

| # | Signal | Target | Source script/run | Owner | Gate § |
|---|--------|--------|-------------------|-------|--------|
| S4 | WS connect p95 @ 15k concurrent | **< 1 s** | k6 metric `flock_ws_connect_duration` (threshold `p(95)<1000`), `load/ws_event_room.js` @ `WS_VUS=15000` | DevOps | §1a / no-go #9 |
| S5 | WS broadcast p95 @ 15k concurrent | **< 1 s** | k6 metric `flock_ws_broadcast_latency` (threshold `p(95)<1000`), same run | DevOps | §1a / no-go #9 |
| S6 | `flock_ws_receive_errors` @ 15k | **== 0** | k6 Counter (threshold `count==0`), same run | DevOps | no-go #9 |
| S7 | Sessions established @ 15k | **100%** | k6 VU connect success from the same run summary | DevOps | no-go #9 |
| S8 | Realtime tier process count post-migrate | **N** socketio workers (not collapsed to 1) | `supervisorctl status socketio-tier` — see [`migration-runbook.md` §4b/§6](migration-runbook.md#6-the-realtime-tier-across-migrations) | DevOps | no-go #3 |

### Migration & restore safety ([FLO-350](/FLO/issues/FLO-350) / [FLO-288](/FLO/issues/FLO-288))

| # | Signal | Target | Source script/run | Owner | Gate § |
|---|--------|--------|-------------------|-------|--------|
| S9 | Migration-drift gate | **green** (no orphan / dangling / duplicate / `execute()`-less patches) | `.github/workflows/ci.yml` "Migration drift gate" step → [`flock_os/tests/test_migration_drift.py`](../../flock_os/tests/test_migration_drift.py) | Arch | no-go #10 |
| S10 | Restore drill | **exit 0** against a real backup; row-count parity across every `Flock %` DocType | [`scripts/dev/restore-drill.sh`](../../scripts/dev/restore-drill.sh) ([`backup-restore.md`](backup-restore.md)) | DevOps | no-go #1 |
| S11 | `--skip-failing` migration debt | none carried into launch (or recorded + follow-up issue filed) | `bench migrate` log; see [`migration-runbook.md` §3](migration-runbook.md#--skip-failing--the-escape-hatch-not-the-default) | Arch | no-go #4 |

### Deploy, smoke & runtime protection

| # | Signal | Target | Source script/run | Owner | Gate § |
|---|--------|--------|-------------------|-------|--------|
| S12 | Staging smoke at promoted tag | **`SMOKE: PASS`** (HTTP/TLS + `ping`→`pong` + WS handshake) | [`scripts/deploy/smoke-staging.sh`](../../scripts/deploy/smoke-staging.sh) ([FLO-250](/FLO/issues/FLO-250)) | DevOps | no-go #2 |
| S13 | Edge rate-limit | active on registration + realtime-connect (Cloudflare) | Cloudflare WAF rule ([FLO-294](/FLO/issues/FLO-294)); complements the app limiter ([FLO-319](/FLO/issues/FLO-319)) | DevOps | no-go #5 |
| S14 | Dedicated adapter Redis in prod | present (separate from cache Redis) | prod topology ([FLO-245](/FLO/issues/FLO-245) / [FLO-127](/FLO/issues/FLO-127) §2) | DevOps | no-go #6 |
| S15 | Observability exercised | dashboards reachable; **≥1 alert fired-and-handled** pre-launch | [FLO-266](/FLO/issues/FLO-266) monitoring | DevOps | §1e |

> **Business signals** (launch partner named + onboarded, budget approved, launch
> date set) are CEO-held and listed in §3 — they are not script-checkable.

## 1. Prerequisites — every Phase 6.1 / 6.2 item, owner + done-criteria

> Owner abbreviations: **Arch** = Architect, **DevOps** = DevOps Engineer,
> **BE** = Backend Engineer, **QA** = QA Engineer. "Status" reflects the issue
> thread at authoring time — re-confirm against the live issue before signing.

### 1a. Deploy pipeline & environments (Phase 6.1)

| Item | Owner | Done-criteria | Status |
|------|-------|---------------|--------|
| [FLO-246](/FLO/issues/FLO-246) Production deploy pipeline (containerized bench on Frappe Cloud Server) | Arch | `master` green → `staging` auto-deploy; image built + pushed; `FLOCK_DEPLOY_CMD` wired on both environments; secret-decrypt + `render-config --check` gate in-workflow | blocked (board endorsement [609ccd5d](/FLO/approvals/609ccd5d-13ca-4db2-8cbc-99069e0224cc)) |
| [FLO-249](/FLO/issues/FLO-249) Staging cloud VM provisioning (Frappe Cloud Server plan + DNS/TLS) | DevOps | [`provision-staging-vm.md` §9 acceptance](../development/provision-staging-vm.md) green: VM up, SSH key on VM, site registered, DNS/TLS, managed DB, **dedicated adapter Redis**, GitHub `staging` environment wired | blocked |
| [FLO-250](/FLO/issues/FLO-250) Live staging smoke + rollback drill (acceptance gate for 6.1) | DevOps | [`staging-preflight-checklist.md`](../development/staging-preflight-checklist.md) walked green; `smoke-staging.sh` → `SMOKE: PASS`; **one rollback drill** (deploy → break → roll back → verify) recorded in the thread | blocked |
| [FLO-251](/FLO/issues/FLO-251) Production promotion gate (manual staging→prod, CEO/QA sign-off) | DevOps | `production` GitHub env has a **required reviewer**; promotion re-deploys the **same tag** that passed staging smoke (exact-artifact parity); prod smoke `PASS` | blocked |

- [ ] FLO-246 — pipeline `master→staging` green end-to-end (evidence link).
- [ ] FLO-249 — staging URL reachable over TLS, pre-flight §1–4 all checked.
- [ ] FLO-250 — staging smoke `PASS` + rollback drill output in thread.
- [ ] FLO-251 — prod promotion path exercised (dry-run or real), reviewer set.

### 1b. Security & permissions (Phase 6.2)

| Item | Owner | Done-criteria | Status |
|------|-------|---------------|--------|
| [FLO-290](/FLO/issues/FLO-290) Security & permission audit (role + row-level isolation across org/branch) | Arch | [`docs/security/permission-audit.md`](../security/permission-audit.md) complete; full role × DocType × position matrix tested; SEC-1..SEC-6 fixed | **done** |
| [FLO-292](/FLO/issues/FLO-292) SEC-A1 — Flock Audit Log append-only (drop Org Admin write/delete) | Arch | Audit Log no longer writable by Org Admin; test covers the restriction | **done** |
| [FLO-293](/FLO/issues/FLO-293) SEC-A2 — Drop Flock Member create on Flock Engagement Session | Arch | Engagement Session can no longer create Flock Member; test covers | **done** |

- [ ] FLO-290 — permission audit doc current; matrix test green.
- [ ] FLO-292 / FLO-293 — hardening merged; regression tests green.

### 1c. Rate limiting (Phase 6.2 + edge)

| Item | Owner | Done-criteria | Status |
|------|-------|---------------|--------|
| [FLO-319](/FLO/issues/FLO-319) App-level rate limits for public endpoints (infra-independent slice of FLO-294) | BE | App-level limiter live on registration/connect endpoints; unit + contract tests green (`test_rate_limit.py`, `test_rate_limit_contract.py`) | **done** |
| [FLO-294](/FLO/issues/FLO-294) SEC-RL — Edge rate-limiting for high-fan-out public endpoints (realtime connect, register) | DevOps | Cloudflare edge rate-limit on the registration + realtime-connect paths (protects the metered Gunicorn quota — hosting-quote risk #3); complements the app-level limiter | blocked |

- [ ] FLO-319 — app limiter active; tests green.
- [ ] FLO-294 — edge rate-limit rule active in Cloudflare; load test shows it trips before Gunicorn budget is at risk.

### 1d. Backup, restore & migration (Phase 6.2)

| Item | Owner | Done-criteria | Status |
|------|-------|---------------|--------|
| [FLO-288](/FLO/issues/FLO-288) Backup & restore drill + runbook | DevOps | [`docs/operations/backup-restore.md`](backup-restore.md) current; `restore-drill.sh` → exit 0 on a **real** (seeded) backup; row-count parity holds across every `Flock %` DocType | **done** |
| [FLO-332](/FLO/issues/FLO-332) Production migration runbook + this go/no-go gate | Arch | [`migration-runbook.md`](migration-runbook.md) covers pre-migrate backup → `bench migrate` (`--skip-failing` rules) → verify → rollback; this checklist covers every 6.1/6.2 prerequisite | **done** |
| [FLO-357](/FLO/issues/FLO-357) Launch go/no-go gate definition (signal catalog + sign-off card) | PM | signal catalog (S1–S15) maps every pre-launch signal → target → source; sign-off card names owners + evidence; no-go conditions explicit | **done** |

- [ ] FLO-288 — drill green against a real backup; archive path + off-host copy proven.
- [ ] FLO-332 — both docs committed; migration runbook references [FLO-245](/FLO/issues/FLO-245) + [FLO-288](/FLO/issues/FLO-288).
- [ ] FLO-357 — signal catalog + sign-off card + no-go conditions present and CEO-reviewed.

### 1e. Observability (Phase 6.2)

| Item | Owner | Done-criteria | Status |
|------|-------|---------------|--------|
| [FLO-266](/FLO/issues/FLO-266) CEO heartbeat monitoring and observability | DevOps | Metrics (app + DB + Redis + WS connections), dashboards, alerting live; at least one alert fired-and-handled recorded before launch | **done** |

- [ ] FLO-266 — dashboards reachable; an alert has been exercised (not just armed).

## 2. Merge-gate / coverage bar (QA holds)

Independent of the issue list — these hold on every deploy and are the QA floor.
The authoritative source is [`scripts/qa-gate.sh`](../../scripts/qa-gate.sh)
(mirrored by `.github/workflows/ci.yml`), which enforces four checks:
`ruff check` → `ruff format --check` → `pytest flock_os/tests/` → branch-coverage
ratchet (`fail_under = 80` in `pyproject.toml`).

- [ ] `master` is green — `.github/workflows/ci.yml` lint + test gate passes on
      the promoted tag.
- [ ] QA gate (`scripts/qa-gate.sh`) is green at the promoted tag — i.e. all four
      checks above pass, and branch coverage is **≥ 91.29%** (the current
      measured bar — do not regress; this is signal S1). The `fail_under = 80`
      ratchet floor in `pyproject.toml` is the CI backstop, **not** the launch
      bar — do not promote below 91.29% even though CI stays green at 80%.
- [ ] No `--skip-failing` migration debt carried into launch (signal S11; see
      [`migration-runbook.md` §3](migration-runbook.md#--skip-failing--the-escape-hatch-not-the-default)).
- [ ] This checklist matches the live issue statuses (no item marked "done" that
      the thread shows as open). QA validates this row before signing.

## 3. Business / launch readiness (CEO holds)

- [ ] **Launch partner named and onboarded** ([FLO-231](/FLO/issues/FLO-231)
      §6 decision #3) — a real org with a real ~15k event. No partner = no real
      launch (degrades to a simulated event per the plan).
- [ ] Hosting budget approved through the launch window
      ([hosting-quote](/FLO/issues/FLO-231#document-hosting-quote): ≈ $500–$1,500).
- [ ] Launch date set; on-call roster + comms plan drafted (Phase 6.4 prereq).

## No-go conditions

**Any one of these is a hard no-go, even if every checklist box above is ticked:**

1. **The restore drill has not run green against a real backup** ([FLO-288](/FLO/issues/FLO-288)).
   No proven rollback target = no launch.
2. **The staging smoke is not `PASS` at the tag being promoted.** Prod never
   gets a tag staging did not pass.
3. **The scaled-socketio tier is not N processes** post-migrate (a collapsed
   single-process tier will not carry 15k WS — see
   [`migration-runbook.md` §6](migration-runbook.md#6-the-realtime-tier-across-migrations)).
4. **`--skip-failing` migration debt is unrecorded or has no follow-up issue.**
   Deferred patches with no owner is silent corruption risk.
5. **Edge rate-limit (FLO-294) is not active on registration + realtime-connect.**
   A viral registration spike bills unbounded Gunicorn CPU (hosting-quote risk #3).
6. **No dedicated adapter Redis** in prod ([FLO-245](/FLO/issues/FLO-245) /
   [FLO-127](/FLO/issues/FLO-127) §2) — the shared `redis_socketio` stalls under
   the 15k burst.
7. **The launch partner is not named / onboarded.** A load test is not a launch.
8. **A required signature (DevOps / QA / CEO) is missing.**
9. **Any §8 scale target is missed at 15k** ([FLO-347](/FLO/issues/FLO-347)):
   WS connect p95 ≥ 1s, WS broadcast p95 ≥ 1s, `flock_ws_receive_errors` > 0,
   or sessions < 100% (signals S4–S7). A regression here means the prod tier
   cannot carry the first real event — re-provision the LB/adapter tier and
   re-run `load/ws_event_room.js` clean before promoting.
10. **The migration-drift gate is red** (signal S9). An orphan, dangling, or
    `execute()`-less patch ships a silently-broken `bench migrate` to prod
    ([FLO-350](/FLO/issues/FLO-350)). Fix the registry and re-run the CI gate.

## Sign-off block

Copy this block into the launch ticket ([FLO-231](/FLO/issues/FLO-231) Phase 6.4)
and fill it at the go/no-go meeting. A signature is an assertion that the
signer's section above is true at the promoted tag.

```
Launch go/no-go — Flock OS first real event
Promoted tag: __________   Date: __________   Launch partner: __________

DevOps   — deploy pipeline + env + rollback drill verified:  [ ] GO  [ ] NO-GO
           name / date: ____________________________________________
QA       — smoke + coverage + checklist-match verified:      [ ] GO  [ ] NO-GO
           name / date: ____________________________________________
CEO      — partner onboarded + budget + business go:         [ ] GO  [ ] NO-GO
           name / date: ____________________________________________
Architect— topology + migration + tier proven (gate author): [ ] GO  [ ] NO-GO
           name / date: ____________________________________________

LAUNCH DECISION: [ ] GO  [ ] NO-GO   (unanimous GO required)
If NO-GO, blockers + re-walk date: _____________________________________
```

## Post-go (hand-off to Phase 6.4)

On a unanimous GO, this gate's evidence feeds the Phase 6.4 first-real-event
run:

- The promoted tag, the smoke outputs, and the restore-drill record become the
  baseline for the event-day on-call runbook.
- The on-call roster + comms plan are finalized (CEO).
- The post-event retrospective ([FLO-231](/FLO/issues/FLO-231) §2 acceptance)
  closes Phase 6 — it records real numbers (peak concurrency, error rate, p95,
  attendance capture rate, anything that broke).

## Related

- Parent strategy: [FLO-231](/FLO/issues/FLO-231) (Phase 6 — Production Launch).
- Gate definition owner: [FLO-357](/FLO/issues/FLO-357) (signal catalog + sign-off card).
- Gate origin: [FLO-332](/FLO/issues/FLO-332) (VM-independent gate + migration runbook).
- Automated companion: [`scripts/launch-gate.sh`](../../scripts/launch-gate.sh) ([FLO-354](/FLO/issues/FLO-354)).
- Scale targets (signals S4–S8): [FLO-347](/FLO/issues/FLO-347) §8 — clean 15k WS gate.
- Hardening (signals S9–S11): [FLO-350](/FLO/issues/FLO-350) — migration-drift + restore drill.
- Production target ADR: [FLO-245](/FLO/issues/FLO-245).
- Migration runbook: [`migration-runbook.md`](migration-runbook.md).
- Backup & restore drill: [`backup-restore.md`](backup-restore.md) ([FLO-288](/FLO/issues/FLO-288)).
- Deploy / rollback: [`docs/development/deploy-runbook.md`](../development/deploy-runbook.md) ([FLO-246](/FLO/issues/FLO-246)).
- Event-day on-call: [`event-day-runbook.md`](event-day-runbook.md) ([FLO-581](/FLO/issues/FLO-581)).
- Incident response: [`incident-runbooks.md`](incident-runbooks.md) ([FLO-694](/FLO/issues/FLO-694)).
- Staging pre-flight: [`docs/development/staging-preflight-checklist.md`](../development/staging-preflight-checklist.md).
- Permission audit: [`docs/security/permission-audit.md`](../security/permission-audit.md) ([FLO-290](/FLO/issues/FLO-290)).
- Pre-production security audit: [`docs/security/pre-production-audit.md`](../security/pre-production-audit.md) ([FLO-682](/FLO/issues/FLO-682)).
- Metrics + alerting design: [`metrics-alerting-design.md`](metrics-alerting-design.md) ([FLO-586](/FLO/issues/FLO-586)).

> **The four operational runbooks** this gate depends on (deploy/rollback,
> event-day, incident) are the deploy/rollback, event-day, and incident links
> above — all cross-linked here per the [FLO-533](/FLO/issues/FLO-533) AC#3
> "four runbooks linked from the go/no-go checklist".
