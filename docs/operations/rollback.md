# Rollback runbook — Flock OS (FLO-895 — Phase 6.2)

> **Definition owner:** [FLO-895](/FLO/issues/FLO-895) (Phase 6.2 — operations
> runbooks). **Parent epic:** [FLO-533](/FLO/issues/FLO-533) (Phase 6.2 —
> observability, security & ops). **Strategy:** [FLO-231](/FLO/issues/FLO-231)
> (Phase 6 — Production Launch).
>
> This is the **operator's rollback runbook** — when to roll back, the rollback
> command, and how to verify it. It covers two cases: a **code/image rollback**
> (the common path — re-deploy `FLOCK_PREVIOUS_TAG`) and a **schema-changing
> rollback** (a deploy that ran migrations — requires a DB restore). The incident-
> trigger summary (when rollback *is* the incident response) lives in
> [`incident-runbooks.md` §5`](incident-runbooks.md#5-deploy-rollback); the
> pipeline internals + the `rollback.sh` script live in
> [`docs/development/deploy-runbook.md`](../development/deploy-runbook.md).

## TL;DR

```bash
# Roll back to the previously-known-good image tag (the common path):
STAGING_URL=https://prod.flock-os.example \
FLOCK_CURRENT_TAG=sha-bad-999 FLOCK_PREVIOUS_TAG=sha-good-888 \
FLOCK_DEPLOY_CMD='TAG=<TAG> docker compose up -d --no-deps --force-recreate bench' \
scripts/deploy/rollback.sh

# If the deploy ran a migration, the image rollback is not enough —
# restore the pre-deploy DB baseline (see §3):
scripts/ops/restore.sh "<§1c-archive>" --site flock_os --confirm --force \
  --db-root-password "$MARIADB_ROOT_PASSWORD" --admin-password "$SITE_ADMIN_PASSWORD"
```

## 1. When to roll back — and when NOT to

### Roll back when

- The [`incident-triage`](dashboards/incident-triage.json) recent-deploy marker
  aligns with the incident start (≤ 30 min), **and**
- The failing tier was healthy pre-deploy (triage-view history confirms), **and**
- The matched response section's immediate actions
  ([`incident-runbooks.md` §1–§4](incident-runbooks.md#1-ws-connection-storm))
  are not stabilizing.

### Do NOT roll back when

- The incident is **capacity exhaustion** on a clean tier
  ([`incident-runbooks.md` §4](incident-runbooks.md#4-15k-event-degradation)) —
  rollback does not add capacity. Use the pause lever
  ([`event-day-runbook.md` §2c](event-day-runbook.md#2c-the-pause-lever-stabilization-not-stop))
  and file a capacity follow-up ([FLO-245](/FLO/issues/FLO-245)).
- The incident is a **single dead worker** ([§1](incident-runbooks.md#1-ws-connection-storm))
  — restart the worker, do not revert the deploy.
- The incident is **organic overload** (the event over-sold vs the 15k budget)
  — shed load, do not roll back.

When in doubt, the decision is the incident commander's — see [`incident.md`](incident.md).
Rollback is itself an incident action; declare the severity first.

## 2. Rollback procedure — image rollback (the common path)

Re-deploys `FLOCK_PREVIOUS_TAG` (or an explicit `--to <tag>`) and re-runs the
smoke. The tags come from the deploy orchestrator's state — the Deploy workflow
records `FLOCK_PREVIOUS_TAG` on the target GH environment after each green
deploy ([`deploy.md` §5](deploy.md#5-health-check--post-deploy)).

```bash
# 1. Confirm the tags (the bad current tag + the known-good previous tag):
#    GitHub → Settings → Environments → production → FLOCK_PREVIOUS_TAG.
#    On the orchestrator host:
docker inspect --format '{{.Config.Image}}' flock-os   # the current (bad) tag

# 2. Roll back — rollback.sh re-deploys the previous tag AND re-runs the smoke:
STAGING_URL=https://prod.flock-os.example \
FLOCK_CURRENT_TAG=sha-bad-999 FLOCK_PREVIOUS_TAG=sha-good-888 \
FLOCK_DEPLOY_CMD='TAG=<TAG> docker compose up -d --no-deps --force-recreate bench' \
scripts/deploy/rollback.sh

# 3. Verify (§4).
```

`FLOCK_CURRENT_TAG` and `FLOCK_PREVIOUS_TAG` are **required** — `rollback.sh`
refuses to run without both, so it can never roll "forward" to the broken tag by
accident. See [deploy-runbook §"How to roll back"](../development/deploy-runbook.md#how-to-roll-back)
for the full script behavior.

> **No migration ran? You're done after §2 + §4.** An image rollback reverts
> code + assets; if the deploy did not run a DB migration, the schema is
> unchanged and the previous tag runs cleanly against the current DB.

## 3. Rollback procedure — schema-changing rollback (DB restore)

If the deploy **ran a migration** (`bench migrate`), an image rollback alone is
**not** enough — the new schema may be incompatible with the previous code, or
the migration may be the regression. Roll back the DB to the pre-deploy baseline
(recorded in [`event-day-runbook.md` §1c](event-day-runbook.md#1c-backup-baseline-the-rollback-target)
for event days, or the deploy pipeline's pre-promotion backup otherwise).

```bash
# 1. Take a safety backup of the CURRENT (bad) state first — never destroy the
#    only copy of the failing state before you can postmortem it:
scripts/ops/backup.sh

# 2. Restore the pre-deploy baseline (the §1c archive):
ARCHIVE="/path/to/pre-deploy-baseline"   # recorded at T-7 / pre-promotion
scripts/ops/restore.sh "$ARCHIVE" --site flock_os --confirm --force \
  --db-root-password "$MARIADB_ROOT_PASSWORD" --admin-password "$SITE_ADMIN_PASSWORD"

#    restore.sh runs `bench migrate` after loading the dump, so the restored
#    schema aligns with the currently-installed flock_os app. See
#    backup-restore.md §"Restoring an archive by hand".

# 3. Re-deploy the previous image tag (§2) so code + DB are consistent.

# 4. Verify (§4).
```

**Non-destructive guarantees** (from [`backup-restore.md`](backup-restore.md)):
`restore.sh` **requires `--confirm`**, and restoring **over a site that already
has data** additionally requires `--force`. A bare invocation always refuses.

> **RPO awareness.** Restoring the pre-deploy baseline loses all writes between
> the baseline backup and the rollback. For an event-day deploy, that window
> includes live attendance — which is why event-day deploys are frozen
> ([`event-day-runbook.md` §1e](event-day-runbook.md#1e-deploy-freeze)) and the
> baseline is taken at T-7. Prefer an image-only rollback (§2) whenever the
> deploy did not migrate.

## 4. Verify the rollback succeeded

```bash
# 1. Smoke the rolled-back tag:
STAGING_URL=https://prod.flock-os.example \
scripts/deploy/smoke-staging.sh --url https://prod.flock-os.example
# Expect [1/4] [2/4] [3/4] [4/4] all PASS.

# 2. Realtime tier count == N (no-go #3):
docker exec flock-os supervisorctl status socketio-tier

# 3. Confirm the live image tag is the rolled-back tag:
docker inspect --format '{{.Config.Image}}' flock-os

# 4. The firing alert(s) returned to green on the incident-triage view.
```

- [ ] Smoke `[4/4]` PASS.
- [ ] Realtime tier count == N.
- [ ] Live image tag == the rolled-back tag (not the bad one).
- [ ] The incident's firing alert(s) green.
- [ ] Post in `#ops` that the rollback landed + the verification (per
      [`incident.md` §4c](incident.md#4c-resolution-on-close)).

## 5. Rollback failed — roll further / escalate

If the rolled-back tag is **also** unhealthy (the smoke fails after rollback),
do **not** re-run with the broken current tag. Options, in order:

1. **Roll further back:** `scripts/deploy/rollback.sh --to <older-tag>` to a tag
   from before the bad deploy's merge window. `--to` accepts any prior tag.
2. **Schema rollback needed but no clean baseline:** restore from the most recent
   **good** off-host backup ([`backup-restore.md` §"Restoring an archive by hand"](backup-restore.md#restoring-an-archive-by-hand)).
   Accept the RPO cost; communicate the data window to the event owner
   ([`incident.md` §4d](incident.md#4d-customer--attendee-facing-event-owner-owns-this)).
3. **Escalate:** if two rollbacks don't recover, escalate to DevOps lead →
   Architect per the [`incident.md` §2](incident.md#2-who-to-page) paging chain.
   This is a Sev1 — the incident commander declares it.

> **Capture the evidence.** The Phase 6.1 acceptance gate records one rollback
> drill (deploy → break → roll back → verify). For a real rollback, capture the
> commands + smoke output in the incident tracking issue (the
> [`incident.md` §5`](incident.md#5-postmortem-template) postmortem input).

## 6. Out of scope

- **The `rollback.sh` script internals + `FLOCK_DEPLOY_CMD` wiring** —
  [`docs/development/deploy-runbook.md` §"How to roll back"](../development/deploy-runbook.md#how-to-roll-back).
- **The incident-trigger rollback decision (when rollback IS the response)** —
  [`incident-runbooks.md` §5`](incident-runbooks.md#5-deploy-rollback).
- **Backup/restore mechanics + the restore drill** —
  [`backup-restore.md`](backup-restore.md).
- **Incident command (severity, paging, postmortem)** — [`incident.md`](incident.md).
- **Event-day freeze + baseline** — [`event-day-runbook.md`](event-day-runbook.md).

## 7. Related

- Parent strategy: [FLO-231](/FLO/issues/FLO-231) (Phase 6 — Production Launch).
- Parent epic: [FLO-533](/FLO/issues/FLO-533) (Phase 6.2 — observability, security & ops).
- Definition owner: [FLO-895](/FLO/issues/FLO-895) (operations runbooks).
- Pipeline internals (deep): [`docs/development/deploy-runbook.md`](../development/deploy-runbook.md) ([FLO-246](/FLO/issues/FLO-246)).
- Deploy: [`deploy.md`](deploy.md) ([FLO-895](/FLO/issues/FLO-895)).
- Incident-trigger rollback: [`incident-runbooks.md` §5`](incident-runbooks.md#5-deploy-rollback) ([FLO-694](/FLO/issues/FLO-694)).
- Incident command: [`incident.md`](incident.md) ([FLO-895](/FLO/issues/FLO-895)).
- Backup + restore: [`backup-restore.md`](backup-restore.md) ([FLO-288](/FLO/issues/FLO-288)).
- Event-day baseline + freeze: [`event-day-runbook.md`](event-day-runbook.md) ([FLO-581](/FLO/issues/FLO-581)).
