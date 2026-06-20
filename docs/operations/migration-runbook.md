# Production migration runbook — Flock OS (FLO-332 Phase 6.2)

> The schema-migration companion to the deploy pipeline. Covers the safe
> `bench migrate` flow for the prod target, the pre-migrate backup, failing-
> patch handling, post-migrate verification, and the rollback path.
>
> **VM-independent:** `bench migrate` mechanics are identical on the local bench
> and in prod. Prove the flow locally first (see §"Local dry-run"), then run the
> same commands against the prod bench. Target environment: a Frappe Cloud
> **Server plan** VM ([FLO-245](/FLO/issues/FLO-245) ADR); topology and secrets
> handling live in [`docs/development/deploy-runbook.md`](../development/deploy-runbook.md).

## TL;DR

```bash
# 0. On the prod VM (or staging), as the bench user, site = flock_os:
cd ~/frappe-bench            # or wherever the prod bench lives
export SITE_NAME=flock_os

# 1. Back the site up FIRST — a migration without a rollback target is a no-go:
scripts/dev/backup.sh                                       # archive under backups/

# 2. Run the migration (skip nothing unless a patch is known-bad — see §3):
bench --site "$SITE_NAME" migrate

# 3. Verify — site loads, realtime tier up, smoke green (§4):
scripts/deploy/smoke-staging.sh --url "$SITE_URL"

# ROLLBACK if any verification step fails — restore the pre-migrate backup (§5):
scripts/dev/restore.sh "<archive-dir>" --site "$SITE_NAME" --confirm --force
```

> The deploy workflow ([FLO-246](/FLO/issues/FLO-246)) runs `bench migrate` as
> entrypoint step 3 on every deploy, so a **normal master→staging→prod** deploy
> already performs a migration. This runbook is the **manual / explicit**
> procedure for: an out-of-band schema change, a failing patch you must reason
> about, a prod-only migration that needs staged verification, or a rollback.

## 1. When a migration runs

| Trigger | Where it runs | Owner |
|---------|---------------|-------|
| `master` merge → staging auto-deploy | deploy workflow entrypoint (`deploy/entrypoint.sh` step 3) | DevOps ([FLO-246](/FLO/issues/FLO-246)) |
| staging → prod promotion | same entrypoint, prod image | DevOps + CEO sign-off ([FLO-251](/FLO/issues/FLO-251)) |
| Out-of-band schema fix / hot patch | **this runbook**, manual SSH to the bench | Architect + DevOps |
| Restore-into-recovery site | `restore.sh` runs `bench migrate` after loading the dump | anyone recovering ([FLO-288](/FLO/issues/FLO-288)) |

`bench migrate` is **idempotent**: Frappe records each patch's run in
`tabPatch Patch Log` and skips it on re-run. Re-running a green migrate is a
no-op for patches; it re-syncs DocType tables, re-runs `after_migrate`, and
clears cache — safe to repeat.

## 2. Pre-migrate backup (non-negotiable)

A migration changes schema and runs data patches. **Never migrate without a
restorable backup** taken immediately before. This is the rollback target.

```bash
# Produces a self-describing archive under $BENCH_DIR/backups/<site>-<ts>/:
#   database.sql.gz + private-files.tar + public-files.tar + site_config.json + manifest.json
scripts/dev/backup.sh

# Record the archive path — this is your rollback target:
MIGRATE_BACKUP=$(ls -1d "$BENCH_DIR"/backups/"$SITE_NAME"-* | tail -1)
echo "Rollback target: $MIGRATE_BACKUP"   # paste this into the deploy ticket
```

- The drill in [`docs/operations/backup-restore.md`](backup-restore.md) proves
  a real backup is restorable; the **same scripts** back prod, so the archive
  you take here is a known-good rollback target (not an article of faith).
- For prod, ship the archive **off-host** before migrating — a backup that lives
  on the same host as the DB is not a rollback target if the host dies.

**Local dry-run (recommended before any prod migration):**

```bash
# Pull the candidate image/code on your local bench, then:
scripts/dev/backup.sh
bench --site flock_os.localhost migrate
scripts/dev/restore-drill.sh     # proves the post-migrate backup is restorable
scripts/qa-gate.sh               # green gate = safe to promote
```

If the drill + qa-gate are green locally, the prod migrate is the same commands
against a different `$SITE_NAME`. Local-first collapses risk: a bad patch fails
on the dev bench, not on prod data.

## 3. The migrate flow

`bench --site <site> migrate` runs, in order:

1. **`[pre_model_sync]` patches** — from `flock_os/patches.txt` (none currently;
   reserved for patches that must run before DocType tables are created).
2. **`sync_all()`** — DocType JSON → `tabDocType` rows + table DDL. This is
   where new/changed columns and tables materialize. Composite indexes that
   DocType JSON cannot express are added by post-sync patches (see
   `flock_os/patches.txt` for the FLO-64 / FLO-11 / FLO-62 / FLO-79 indexes).
3. **`[post_model_sync]` patches** — the Flock patches that touch tables/indexes:
   `seed_core_fixtures`, `seed_gathering_fixtures`, `add_attendance_indexes`,
   `ensure_attendance_name_sequence_default`, `add_engagement_indexes`,
   `add_registration_indexes`, `add_invitation_indexes`.
4. **Fixtures / custom fields / custom scripts** sync.
5. **`after_migrate` hooks** — `flock_os.utils.realtime_setup` re-arms the three
   realtime wire scripts (handler, auth cache, **redis adapter**). This is what
   keeps the FLO-121 scaled-socketio tier alive across a framework upgrade that
   rewrites `index.js` (see §6).
6. **Cache clear** + (optional) website search-index rebuild.

```bash
# Standard migration — run everything:
bench --site "$SITE_NAME" migrate

# Skip the website search-index rebuild (faster; only when no website change shipped):
bench --site "$SITE_NAME" migrate --skip-search-index
```

### `--skip-failing` — the escape hatch, not the default

A failing patch aborts the whole migrate by default (loud, correct — a half-run
migrate is a broken site). `--skip-failing` logs the failing patch and continues:

```bash
bench --site "$SITE_NAME" migrate --skip-failing
```

**Treat `--skip-failing` as a last resort, not a routine flag.** Each skipped
patch is a deferred data/schema fix; a skipped patch that a later patch or
DocType depends on corrupts silently. Rules:

- **Default:** let a failing patch abort. Read the traceback, fix the patch (or
  the data it tripped on), re-run `bench migrate` (patches are idempotent).
- **Only use `--skip-failing` when:** (a) the failing patch is *known* to be
  safe to defer (e.g. an index-rebuild patch on a column you will recreate
  next release), (b) you have recorded the skipped patch id in the deploy
  ticket, and (c) a follow-up issue is filed to re-run it before the next
  event-day.
- **Never** stack `--skip-failing` across releases. Skipped patches accumulate;
  the migration debt must be paid before promoting to prod.

After any `--skip-failing` run, capture which patches were skipped:

```bash
# Patches that ran vs. failed this migrate land in the bench log; grep for FAIL:
bench --site "$SITE_NAME" migrate --skip-failing 2>&1 | tee /tmp/migrate-$(date +%s).log
grep -iE 'FAIL|skip|error' /tmp/migrate-*.log | tail -20
```

## 4. Post-migrate verification

A migrate is not done until the site is proven healthy. In order:

```bash
# 4a. The bench doctor passes (boot, db, redis, site config all wired):
bench --site "$SITE_NAME" doctor

# 4b. The realtime tier is armed — after_migrate re-inserted the adapter marker.
#     A missing marker raises RealtimeWiringError on boot; if the site booted,
#     the marker is present. Confirm the adapter process count is N, not 1:
docker exec flock-os supervisorctl status socketio-tier  # container
#   or: sudo supervisorctl status socketio-tier            # bare VM

# 4c. Smoke — the same three-probe gate the deploy workflow runs:
scripts/deploy/smoke-staging.sh --url "$SITE_URL"
```

- **4a `doctor`** confirms the site boots cleanly post-schema-change. A failure
  here usually means a patch left a column/table inconsistent — restore (§5).
- **4b** confirms the scaled-socketio tier survived the migrate. The
  `after_migrate` hook re-arms the adapter; if `socketio-tier` shows a single
  process instead of N, restart it:
  `docker exec flock-os supervisorctl restart socketio-tier`
  (see [`deploy-runbook.md` → scaled-socketio tier](../development/deploy-runbook.md#the-flo-121-scaled-socketio-tier-across-deploys)).
- **4c smoke** must end in **`SMOKE: PASS`** (all three probes: HTTP/TLS, `ping`
  → `pong`, WS handshake). A smoke `FAIL` is a no-go for prod promotion —
  restore (§5) and triage via
  [`deploy-runbook.md` → Troubleshooting](../development/deploy-runbook.md#troubleshooting).

### Acceptance evidence

For a prod migration, paste into the launch go/no-go
([`launch-go-no-go.md`](launch-go-no-go.md)) and the deploy ticket:

- [ ] pre-migrate backup path recorded (off-host copy confirmed for prod).
- [ ] `bench migrate` exit 0 (or `--skip-failing` with skipped-patch list +
      follow-up issue, signed off by the Architect).
- [ ] `bench doctor` green.
- [ ] `socketio-tier` shows N processes.
- [ ] `smoke-staging.sh` → `SMOKE: PASS`.

## 5. Rollback path

If **any** verification step fails, roll back to the pre-migrate backup. Do not
attempt to "patch forward" on a prod site you have not proven healthy — the
backup is your known-good state.

```bash
# Restore the pre-migrate archive over the prod site (destructive → needs flags):
scripts/dev/restore.sh "$MIGRATE_BACKUP" \
    --site "$SITE_NAME" --confirm --force \
    --db-root-password "$MARIADB_ROOT_PASSWORD" \
    --admin-password "$SITE_ADMIN_PASSWORD"

# restore.sh runs bench migrate after loading the dump so the restored schema
# aligns with the installed flock_os; since we restored the *pre*-migrate backup
# against the *current* image, that migrate re-applies the patches we are rolling
# back from. To roll back CODE too, deploy the previous image tag first:
#   scripts/deploy/rollback.sh   # re-deploys FLOCK_PREVIOUS_TAG (see deploy-runbook)

# Re-verify after rollback:
bench --site "$SITE_NAME" doctor
scripts/deploy/smoke-staging.sh --url "$SITE_URL"
```

`restore.sh` is non-destructive by default: it **requires `--confirm`**, and
**requires `--force`** to overwrite a site that already has data (bench restore
drops + recreates the DB). See [`backup-restore.md` → Restoring an archive by
hand](backup-restore.md#restoring-an-archive-by-hand) for the full flag
reference and the recovery-site (rename) variant.

> **Code + schema must roll back together.** A schema rollback without the code
> rollback leaves the image expecting columns the restored dump removed (or vice
> versa). Always run `scripts/deploy/rollback.sh` to the **previous image tag**
> in the same step, so image + schema match the last-known-good state.

## 6. The realtime tier across migrations

The FLO-121 scaled-socketio tier (N node workers behind the nginx sticky-L7 LB)
is **self-healing across `bench migrate`**:

1. `bench migrate` → step 5 runs the `after_migrate` hook
   (`flock_os.utils.realtime_setup`).
2. The hook re-runs the three idempotent, marker-guarded wire scripts:
   `rewire_socketio_handler`, `rewire_socketio_auth_cache`,
   `rewire_socketio_redis_adapter`.
3. Each fails **loud** (`RealtimeWiringError`) if its marker is missing — a
   framework upgrade that rewrote `index.js` is caught here, not silently
   shipped. If a migrate fails at this step, the adapter block needs re-inserting
   before the tier boots (the wiring script does it; investigate why the marker
   guard fired).

This is why §4b is a verification step, not an assumption: a green migrate
*should* leave the tier armed, but confirm the process count before declaring
the site healthy.

## 7. Troubleshooting

| Symptom | Cause / Fix |
|---------|-------------|
| Migrate aborts on a patch (traceback) | Default behavior — a patch is broken. Read the traceback, fix the patch or the data, re-run `bench migrate` (idempotent). Only use `--skip-failing` under the §3 rules. |
| `RealtimeWiringError` during `after_migrate` | A wire script's marker guard fired — `index.js` shape changed. Re-run `scripts/dev/wire-socketio-redis-adapter.sh` by hand, confirm the marker, then `bench --site <site> migrate` again. |
| Post-migrate `doctor` fails (boot error) | A patch left schema inconsistent. Restore the pre-migrate backup (§5); do not patch forward blind. |
| Smoke `[3/3] FAIL` (WS handshake) | The socketio tier collapsed to 1 process. `supervisorctl restart socketio-tier`; if it stays collapsed, the adapter marker is missing (see row above). |
| Smoke `[2/3] FAIL` (`ping` ≠ `pong`) | gunicorn up but site config broken — usually a render-config issue post-restore. Check `site_config.json` rendered; see [`deploy-runbook.md` → Render site config](../development/deploy-runbook.md#render-site-config-manual). |
| `--skip-failing` used; site seems fine | Not done — file the follow-up issue for the skipped patch and add it to the next go/no-go. A skipped patch is migration debt until re-run green. |

## Out of scope

- Backup/restore mechanics, retention, the restore drill →
  [`docs/operations/backup-restore.md`](backup-restore.md) ([FLO-288](/FLO/issues/FLO-288)).
- Deploy / rollback orchestrator commands (`FLOCK_DEPLOY_CMD`, image tags) →
  [`docs/development/deploy-runbook.md`](../development/deploy-runbook.md) ([FLO-246](/FLO/issues/FLO-246)).
- Secrets (SOPS+age) rendering →
  [`docs/development/secrets-runbook.md`](../development/secrets-runbook.md).
- The launch sign-off gate itself →
  [`docs/operations/launch-go-no-go.md`](launch-go-no-go.md).

## Related

- Parent strategy: [FLO-231](/FLO/issues/FLO-231) (Phase 6 — Production Launch).
- Production target ADR: [FLO-245](/FLO/issues/FLO-245).
- Deploy pipeline (entrypoint that runs this migrate on each deploy):
  [FLO-246](/FLO/issues/FLO-246).
- Backup & restore drill (the rollback-target proof): [FLO-288](/FLO/issues/FLO-288).
- Launch go/no-go gate: [`launch-go-no-go.md`](launch-go-no-go.md).
