# Production deploy runbook — Flock OS (FLO-895 — Phase 6.2)

> **Definition owner:** [FLO-895](/FLO/issues/FLO-895) (Phase 6.2 — operations
> runbooks). **Parent epic:** [FLO-533](/FLO/issues/FLO-533) (Phase 6.2 —
> observability, security & ops). **Strategy:** [FLO-231](/FLO/issues/FLO-231)
> (Phase 6 — Production Launch).
>
> This is the **operator's promotion runbook** — the steps to take a tag from
> staging-green to prod, verify it, and leave the system rollback-ready. The
> **pipeline internals** (image build, `FLOCK_DEPLOY_CMD` wiring, secret
> rendering, nginx sticky-L7, the asset-build mandate) live in the deep
> [`docs/development/deploy-runbook.md`](../development/deploy-runbook.md)
> ([FLO-246](/FLO/issues/FLO-246)); this doc is the on-call's actionable
> summary, cross-linked to it.
>
> **Two-stage pipeline.** Master green → staging auto-deploys; staging smoke
> green → prod is a **manual promotion gate** (CEO/QA reviewer). The prod
> promotion redeploys the **same tag** that passed staging — exact artifact
> parity, no rebuild.

## TL;DR

```bash
# Staging auto-deploys on master-green (the Deploy workflow does it).
# Promote staging → prod (manual gate):
#   GitHub → Actions → "Deploy" workflow → Run workflow → check "promote_to_prod".

# Smoke the target (staging or prod):
STAGING_URL=https://staging.flock-os.example \
scripts/deploy/smoke-staging.sh --url https://staging.flock-os.example

# Record the rollback target after a green deploy (see §5):
echo "FLOCK_PREVIOUS_TAG=$(git rev-parse --short HEAD)"  # set on the staging GH environment
```

## 1. Pre-deploy checklist

- [ ] **Master is green** — the `CI` workflow (lint + test + migration-drift +
      coverage floor) passed on the commit being promoted.
- [ ] **Staging smoke green** on the exact tag you're promoting — including the
      `[4/4]` engagement-asset check ([FLO-617](/FLO/issues/FLO-617)). If staging
      is not healthy, prod promotion is blocked.
- [ ] **Secrets render green** — `scripts/deploy/render-config.sh --check` exits
      0 against the target SOPS bundle (`secrets/prod.enc.yaml`). A missing
      required var fails the deploy loudly.
- [ ] **Rollback target known** — `FLOCK_PREVIOUS_TAG` is set on the target GH
      environment (recorded after the last green deploy). If unknown, recover it
      before promoting (see [`rollback.md`](rollback.md)).
- [ ] **Not in an event window** — no promotion during the
      [`event-day-runbook.md` §1e](event-day-runbook.md#1e-deploy-freeze) freeze
      (T-2 → T+24h) unless the on-call approves an emergency promotion.
- [ ] **Restore drill fresh** — `scripts/dev/restore-drill.sh` exit 0 within the
      last release cycle (no-go #1 — see [`backup-restore.md`](backup-restore.md)).

## 2. Deploy — staging (automatic)

Staging is the canary; no manual step unless it breaks.

1. A slice merges into `master` (the per-slice worktree workflow —
   [`docs/development/per-slice-worktrees.md`](../development/per-slice-worktrees.md)).
2. `CI` runs the lint+test gate.
3. CI green → the `Deploy` workflow builds + pushes the `flock-os-bench` image,
   runs the `render-config.sh --check` secret gate, then invokes
   `FLOCK_DEPLOY_CMD` for the `staging` environment.
4. **Post-deploy smoke** (`scripts/deploy/smoke-staging.sh`) runs against
   `$STAGING_URL` — the four checks: HTTP 2xx/3xx, Frappe `ping=pong`, WS
   handshake, engagement assets 200 (`[4/4]`). A failure fails the workflow;
   staging is NOT healthy and prod promotion is blocked.
5. On success, the tag is recorded as `FLOCK_PREVIOUS_TAG` on the `staging`
   environment — **this is the rollback target for the next deploy**.

If staging smoke fails, do **not** promote. Diagnose via the
[deploy-runbook troubleshooting table](../development/deploy-runbook.md#troubleshooting)
— the most common is `[4/4] FAIL` (asset build skipped / web worker not restarted
after `bench build --app flock_os`).

## 3. Deploy — production (manual promotion gate)

1. **Confirm staging is green** on the tag you're promoting (§2 step 4/5).
2. **A required reviewer on the `production` environment** (CEO/QA) approves the
   promotion: GitHub → Actions → "Deploy" → Run workflow → check `promote_to_prod`.
3. The workflow promotes the **same tag that passed the staging smoke** (no
   rebuild — exact artifact parity) and runs the prod smoke
   (`scripts/deploy/smoke-staging.sh` against `$PROD_URL`).
4. On green, record the promoted tag as `FLOCK_PREVIOUS_TAG` on the `production`
   environment (§5). The previous prod tag becomes the rollback target.

> **The promotion is manual for a reason.** The gate exists so a human confirms
> the staging-canary evidence before prod takes traffic. Do not bypass it (e.g.
> by running `FLOCK_DEPLOY_CMD` by hand) — a hand promotion skips the smoke and
> the rollback-tag recording, leaving you without a clean rollback target.

## 4. Site setup — bench, workers, socketio, scheduler

The unit of deploy is the **`flock-os-bench` container image** — a complete
Frappe bench with the [FLO-121](/FLO/issues/FLO-121) scaled-socketio tier baked
in. On a standard image recreate, all of the below come up with the container.
The sections below are the **manual / bare-VM** path (a `bench update` or
`git pull && bench restart`), and the verification commands for any path.

```bash
cd /home/frappe/frappe-bench          # BENCH_DIR

# 1. Build flock_os assets (MANDATORY — the [4/4] smoke enforces it):
bench build --app flock_os

# 2. Apply migrations (the entrypoint runs this; on a bare VM run it by hand):
bench --site flock_os migrate

# 3. Restart processes:
bench restart
# — OR, under supervisor (the prod container shape) —
supervisorctl restart gunicorn        # web only
supervisorctl restart socketio-tier   # the scaled WS tier (N workers + nginx LB)
supervisorctl restart bench-worker    # bench worker (queues)
supervisorctl restart bench-schedule  # scheduler
```

**Verify the scaled-socketio tier** (the dominant scale axis — no-go #3 / S8):

```bash
docker exec flock-os supervisorctl status socketio-tier
# Expect N node backends + the nginx LB running. A collapse to a single
# socketio is no-go #3 — see docs/development/deploy-runbook.md "The FLO-121
# scaled-socketio tier across deploys".
```

If a dashboard restart collapses the tier to a single socketio, SSH in and:

```bash
docker exec -it flock-os supervisorctl restart socketio-tier
# or, bare-VM: sudo supervisorctl restart socketio-tier
```

> **Why the asset build + web-worker restart is mandatory.** `bench build --app
> flock_os` collects `flock_os/public/*` into `sites/assets/flock_os/`. A
> gunicorn that booted before the build still 404s newly-added asset dirs. The
> container image path bakes the build in before gunicorn boots; a bare-VM path
> must run it explicitly. Full rationale:
> [deploy-runbook §"Asset build + web-worker restart"](../development/deploy-runbook.md#asset-build--web-worker-restart-flo-617).

## 5. Health check + post-deploy

```bash
# 1. Run the four-check smoke against the live URL:
STAGING_URL=https://prod.flock-os.example \
scripts/deploy/smoke-staging.sh --url https://prod.flock-os.example
# Expect: [1/4] [2/4] [3/4] [4/4] all PASS.

# 2. Confirm the realtime tier count == N (no-go #3):
docker exec flock-os supervisorctl status socketio-tier

# 3. Record the rollback target — set FLOCK_PREVIOUS_TAG on the target GH
#    environment to the just-promoted tag (so the NEXT deploy can roll back to
#    this one). The Deploy workflow does this automatically; on a manual path,
#    set it in GitHub Settings → Environments → production.
```

- [ ] Smoke `[4/4]` PASS against prod.
- [ ] Realtime tier count == N.
- [ ] `FLOCK_PREVIOUS_TAG` recorded.
- [ ] If this was the first real-event deploy, the
      [`launch-go-no-go.md`](launch-go-no-go.md) gate is walked and signed.

## 6. When a deploy goes wrong → rollback

If the prod smoke fails, or an incident starts ≤ 30 min after a promotion, go to
[`rollback.md`](rollback.md) (operator) /
[`incident-runbooks.md` §5](incident-runbooks.md#5-deploy-rollback)
(incident-trigger summary). **Do not** re-run the broken promotion; roll back to
`FLOCK_PREVIOUS_TAG` first, then diagnose.

## 7. Out of scope

- **Pipeline internals** (Dockerfile, `FLOCK_DEPLOY_CMD` patterns, secret
  rendering, nginx sticky-L7, troubleshooting table) —
  [`docs/development/deploy-runbook.md`](../development/deploy-runbook.md).
- **Secrets editing/rotation** — [`docs/development/secrets-runbook.md`](../development/secrets-runbook.md).
- **Rollback procedure** — [`rollback.md`](rollback.md).
- **Incident-trigger rollback decision** — [`incident-runbooks.md` §5](incident-runbooks.md#5-deploy-rollback).
- **Event-day deploy freeze** — [`event-day-runbook.md` §1e](event-day-runbook.md#1e-deploy-freeze).

## 8. Related

- Parent strategy: [FLO-231](/FLO/issues/FLO-231) (Phase 6 — Production Launch).
- Parent epic: [FLO-533](/FLO/issues/FLO-533) (Phase 6.2 — observability, security & ops).
- Definition owner: [FLO-895](/FLO/issues/FLO-895) (operations runbooks).
- Pipeline internals (deep): [`docs/development/deploy-runbook.md`](../development/deploy-runbook.md) ([FLO-246](/FLO/issues/FLO-246)).
- Rollback: [`rollback.md`](rollback.md) ([FLO-895](/FLO/issues/FLO-895)).
- Secrets: [`docs/development/secrets-runbook.md`](../development/secrets-runbook.md) ([FLO-248](/FLO/issues/FLO-248)).
- Backup + restore: [`backup-restore.md`](backup-restore.md) ([FLO-288](/FLO/issues/FLO-288)).
- Launch gate: [`launch-go-no-go.md`](launch-go-no-go.md) ([FLO-357](/FLO/issues/FLO-357)).
- Incident response: [`incident.md`](incident.md) / [`incident-runbooks.md`](incident-runbooks.md) ([FLO-694](/FLO/issues/FLO-694)).
- Event-day freeze: [`event-day-runbook.md`](event-day-runbook.md) ([FLO-581](/FLO/issues/FLO-581)).
