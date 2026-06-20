# Launch go/no-go readiness gate — Flock OS (FLO-332 Phase 6.2)

> The sign-off checklist **DevOps + QA + CEO** walk together before promoting the
> first **real** event to production. Every Phase 6.1 (deploy pipeline) and 6.2
> (launch-readiness hardening) prerequisite is a line item with an **owner** and
> **done-criteria**. If every box is checked, the launch is a **go**. If any
> **no-go condition** (§"No-go conditions") holds, the launch is a **no-go** —
> fix the item and re-walk this gate.
>
> **This gate is VM-independent as a document** (it can be drafted and reviewed
> before infra exists) but **the launch itself is gated on real evidence**:
> green smokes, a green restore drill, and the three signatures. Stubs/TODOs in
> the runbooks are no-go on their own.

## TL;DR — the decision

| Role | Signs off on | Gate |
|------|--------------|------|
| **DevOps** | Deploy pipeline green end-to-end, prod env reachable + healthy, rollback drill recorded | §1 |
| **QA** | Smoke gates green, coverage bar held, checklist matches the merge-gate reality | §1 + §2 |
| **CEO** | Launch partner named + onboarded, budget approved, business go | §3 |
| **Architect** | Topology + migration + realtime tier proven; this checklist is complete + accurate | (gate author) |

**A launch needs all four.** A missing signature is a no-go.

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
| [FLO-332](/FLO/issues/FLO-332) Production migration runbook + this go/no-go gate | Arch | [`migration-runbook.md`](migration-runbook.md) covers pre-migrate backup → `bench migrate` (`--skip-failing` rules) → verify → rollback; this checklist covers every 6.1/6.2 prerequisite | **in_progress** (this issue) |

- [ ] FLO-288 — drill green against a real backup; archive path + off-host copy proven.
- [ ] FLO-332 — both docs committed; migration runbook references [FLO-245](/FLO/issues/FLO-245) + [FLO-288](/FLO/issues/FLO-288).

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
      checks above pass, and branch coverage is **≥ 80%** (the ratchet floor; the
      foundation currently measures ~91% actual). This 80% is the launch coverage
      bar — do not promote below it.
- [ ] No `--skip-failing` migration debt carried into launch (see
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
- Production target ADR: [FLO-245](/FLO/issues/FLO-245).
- Migration runbook: [`migration-runbook.md`](migration-runbook.md).
- Backup & restore drill: [`backup-restore.md`](backup-restore.md) ([FLO-288](/FLO/issues/FLO-288)).
- Deploy / rollback: [`docs/development/deploy-runbook.md`](../development/deploy-runbook.md) ([FLO-246](/FLO/issues/FLO-246)).
- Staging pre-flight: [`docs/development/staging-preflight-checklist.md`](../development/staging-preflight-checklist.md).
- Permission audit: [`docs/security/permission-audit.md`](../security/permission-audit.md) ([FLO-290](/FLO/issues/FLO-290)).
