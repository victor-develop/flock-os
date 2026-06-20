# Backup & restore runbook — Flock OS (FLO-288 Phase 6.2)

> The operational companion to `scripts/dev/backup.sh`,
> `scripts/dev/restore.sh`, and `scripts/dev/restore-drill.sh`. Covers backup
> strategy, the restore drill, retention, RPO/RTO, and prod-vs-local notes.
>
> **VM-independent:** `bench backup --with-files` / `bench restore` mechanics are
> identical on the local bench and in prod, so the same scripts are the single
> backup/restore entry point for both. Prove it locally with the restore drill
> before every release ([FLO-231](/FLO/issues/FLO-231) §2: "a restore drill
> succeeds from a real backup").

## TL;DR

```bash
# 1. Back the site up (archive lands under $BENCH_DIR/backups/<site>-<timestamp>/):
scripts/dev/backup.sh

# 2. Restore into a target site (non-destructive; needs --confirm, + --force over data):
scripts/dev/restore.sh "<archive-dir>" --site <target>.localhost --confirm

# 3. Run the full restore drill (backup → fresh drill site → row-count parity):
scripts/dev/restore-drill.sh
```

The scripts auto-load `$REPO_ROOT/.env` for `BENCH_DIR` / `SITE_NAME` /
`MARIADB_ROOT_PASSWORD` / `SITE_ADMIN_PASSWORD`; every value is overridable with a
flag. The `.env` is gitignored and never committed.

## What is backed up

A backup archive (`$BENCH_DIR/backups/<site>-<timestamp>/`) is self-describing:

| File | Contents |
|------|----------|
| `<site>-database-<ts>.sql.gz` | Full MariaDB dump (`bench backup`) |
| `<site>-private-files-<ts>.tar` | `sites/<site>/private/files/` |
| `<site>-public-files-<ts>.tar` | `sites/<site>/public/files/` |
| `site_config.json` | Site config (incl. DB credentials — needed to restore) |
| `manifest.json` | File list + sha256, app versions, timestamp, bench path |

The archive lives **outside the repo** (under `$BENCH_DIR/backups`). It contains
secrets (`site_config.json`) and uploaded files, so it **must never be committed**.
Keep backups on the tracked-tree-adjacent storage only; `.gitignore` already
excludes the bench tree.

## Backup schedule & retention

| Environment | Frequency | Mechanism | Retention |
|-------------|-----------|-----------|-----------|
| Local bench | On demand (before risky changes, before a release) | `scripts/dev/backup.sh` | Developer-managed; prune `$BENCH_DIR/backups/` by hand |
| Staging | Daily + pre-deploy | cron / deploy pipeline (wire on [FLO-246](/FLO/issues/FLO-246) landing) | 7 daily |
| Production | Daily (off-peak) + before each deploy | deploy pipeline + off-host copy | 30 daily + 12 monthly (RPO target ≤ 24h) |

> **Retention is policy, not yet automation.** The deploy pipeline
> ([FLO-246](/FLO/issues/FLO-246)) will wire scheduled backups + off-host copies
> and the rotation above once it lands (currently awaiting board endorsement —
> [609ccd5d](/FLO/approvals/609ccd5d-13ca-4db2-8cbc-99069e0224cc)). The scripts
> here are the integration point: the pipeline calls `backup.sh` and ships the
> archive off-host. Do **not** block this runbook on the pipeline — the local
> drill is valuable and proven now.

### RPO / RTO guidance

- **RPO** (acceptable data loss): ≤ 24h for prod (daily backup). Tighten to
  hourly via the pipeline if event-day traffic warrants it.
- **RTO** (acceptable downtime to recover): target ≤ 1h for prod — `restore.sh`
  + `bench migrate` + smoke is the measured path; the drill bounds it.
- The drill's row-count parity step is the RTO confidence gate: if parity holds
  against a real backup, the restore path is known-good.

## The restore drill

The drill ([FLO-231](/FLO/issues/FLO-231) Phase 6 acceptance) backs the live
site up, restores it into a throwaway `flock_os_restore_drill.localhost` site,
and asserts **row-count parity** across every `Flock *` DocType present in the
source. A green run proves a real backup is restorable.

```bash
# From the repo root, with .env providing BENCH_DIR / SITE_NAME / MARIADB_ROOT_PASSWORD:
scripts/dev/restore-drill.sh

# Inspect the restored site instead of dropping it:
scripts/dev/restore-drill.sh --keep
# then: bench --site flock_os_restore_drill.localhost drop-site --yes
```

What the drill checks:

1. **Backs up** the source site via `backup.sh`.
2. **Restores** into a fresh (or `--force`-overwritten) `flock_os_restore_drill`
   site via `restore.sh --confirm --force`.
3. **Compares row counts** for every `Flock %` DocType (auto-discovered from
   `tabDocType`) between source and restored — via raw SQL, so it is
   **app/version-independent**: whatever DocTypes the backed-up release ships,
   the restored site must match. (The ticket's named core set — `Flock Branch`,
   `Flock Group`, `Flock Member`, `Flock Gathering` — is always covered; later
   DocTypes like `Flock Event Registration` join automatically once merged.)

Exit code `0` = parity holds (drill site dropped automatically). Non-zero =
divergence report printed; the drill site is dropped unless `--keep`.

### Rehearsing against the prod-equivalent docker-compose topology

The drill is stack-agnostic: the same `restore-drill.sh` runs against the
host bench (DB host `127.0.0.1`) and against the [FLO-347](/FLO/issues/FLO-347)
docker-compose prod-equivalent tier (DB host `mariadb` on the docker network).
The DB host is resolved at runtime from `site_config.json` (`host`) → the
bench-level `common_site_config.json` (`db_host`) → `127.0.0.1`, so no flag
edit is needed to switch topology.

```bash
# 1. Bring up the prod-equivalent tier (MariaDB + Redis AOF + gunicorn + WS LB):
scripts/dev/docker-ws-tier.sh up

# 2. Run the drill INSIDE the web container (bench-in-container == prod shape):
docker compose -f docker/docker-compose.yml exec web \
  bash apps/flock_os/scripts/dev/restore-drill.sh \
    --bench-dir /home/frappe/frappe-bench \
    --source-site flock_os.localhost \
    --db-root-password "$MARIADB_ROOT_PASSWORD" \
    --admin-password "$SITE_ADMIN_PASSWORD"
#    (creds come from docker/.env.docker, already in the container env)

# 3. Tear the tier down (Redis AOF volumes + MariaDB volume persist unless -v):
scripts/dev/docker-ws-tier.sh down
```

The docker tier's `redis-cache` + `redis-queue` are **AOF-persisted**
(`--appendonly yes --appendfsync everysec`) on named volumes, so Frappe
session/cache + queue state survive a container restart/recreate — the
Redis-persistence half of this runbook's backup/restore story. The dedicated
`redis-adapter` is intentionally ephemeral (transient socket.io pub/sub).

### Non-destructive guarantees

- `restore.sh` **requires `--confirm`** — a bare invocation always refuses.
- Restoring **over a site that already has data** additionally **requires
  `--force`** (bench restore drops + recreates the DB). Without it, `restore.sh`
  refuses and names the flag to set.
- The drill only ever touches the throwaway `flock_os_restore_drill` site (created
  fresh or `--force`-overwritten), never the live site.

## Restoring an archive by hand

```bash
# Restore the latest prod backup into a fresh recovery site:
ARCHIVE=$(ls -1d $BENCH_DIR/backups/flock_os.localhost-* | tail -1)
scripts/dev/restore.sh "$ARCHIVE" --site recovery.localhost --confirm \
  --db-root-password "$MARIADB_ROOT_PASSWORD" --admin-password "$SITE_ADMIN_PASSWORD"

# Then repoint web traffic at recovery.localhost (or rename the site):
bench --site recovery.localhost use
```

`restore.sh` runs `bench migrate` after loading the dump so the restored schema
aligns with the currently-installed `flock_os` app (no-op when versions match).

## Prod vs local

| Concern | Local bench | Prod |
|---------|-------------|------|
| Where archives land | `$BENCH_DIR/backups/` | Off-host (S3 / volume snapshot) via the deploy pipeline |
| Off-host copy | Not needed (dev only) | **Required** — a backup on the same host as the DB is not a backup |
| Encryption at rest | Local disk only | The archive contains `site_config.json` (DB creds); encrypt the off-host copy (SOPS/age, see [secrets-runbook](../development/secrets-runbook.md)) |
| Drill | Run before each release | Run in staging on a prod-derived backup before promoting |
| Root DB password | `.env` `MARIADB_ROOT_PASSWORD` | Secret manager → render-config (see [deploy-runbook](../development/deploy-runbook.md)) |

## Verification checklist (per release)

- [ ] `scripts/dev/restore-drill.sh` exits 0 against the seeded local site.
- [ ] A fresh backup's `manifest.json` lists DB + private + public files with
      sha256.
- [ ] `scripts/qa-gate.sh` is green (the drill's pure logic is unit-covered in
      `flock_os/tests/test_backup_drill.py`).
- [ ] No secrets in the repo (archives live under `$BENCH_DIR`, gitignored).

## Related

- Parent strategy: [FLO-231](/FLO/issues/FLO-231) (Phase 6 — Production Launch).
- Deploy pipeline (integration point for scheduled backups):
  [FLO-246](/FLO/issues/FLO-246).
- Secrets (SOPS+age): [docs/development/secrets-runbook.md](../development/secrets-runbook.md).
- Deploy/rollback: [docs/development/deploy-runbook.md](../development/deploy-runbook.md).
