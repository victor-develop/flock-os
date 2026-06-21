# MVP operational runbook — Flock OS

> Source: [FLO-530](/FLO/issues/FLO-530). The first ops-runbook slice: a single
> reference for **core administration** and **troubleshooting** of the Flock OS
> MVP (a Frappe custom app on MariaDB + Redis). Use this when the site is
> misbehaving, when onboarding a new operator, or before any manual prod-ish
> action. It is deliberately **local-bench first** — every command here works on
> the dev bench, and the same `bench` mechanics carry to prod unchanged.
>
> This document is the **index** for routine operations; it links out to the
> specialised runbooks instead of duplicating them. If you can only read one
> section, read [Triage cheat sheet](#triage-cheat-sheet).

## Scope and assumptions

| Item | Value |
| --- | --- |
| App | `flock_os` (Frappe custom app), version in `flock_os/__init__.py` (`__version__`) |
| Framework | Frappe v15 (Frappe Bench) |
| DB | MariaDB (Homebrew service on this Mac; TCP user `frappe_root`) |
| Cache / queue / realtime | Redis (Homebrew service on this Mac) |
| Site name | `flock_os.localhost` (override via `$SITE_NAME`) |
| Bench runtime | **Outside the repo**, default `/Users/mac/opencode-workspace-default/flock-os-bench` (`$BENCH_DIR` in `.env`) |
| Secrets | Local-only, gitignored `.env`. Never committed, never pasted into agent prompts. See [Secrets handling](#secrets-handling) |
| Operator model | **AI company on Paperclip**. Most "operations" are performed by agents in heartbeats. This runbook is the deterministic reference any agent (or board operator) follows |

> **Prod caveat.** At MVP time there is no production environment yet — Phase 6
> launches it ([FLO-231](/FLO/issues/FLO-231)). Commands here are bench-proven;
> the prod-specific differences (off-host backups, secrets manager, deploy
> pipeline) are flagged inline and tracked by [FLO-246](/FLO/issues/FLO-246).
> Where a prod step is not yet wired, the runbook says so rather than inventing
> one.

## Triage cheat sheet

When something is wrong, run this in order. Each step links to the deeper
section.

```bash
# 1. Are the platform services up?  (expect "PONG" and "OK")
redis-cli ping
mysql -u frappe_root -h 127.0.0.1 -P 3306 -p"$MARIADB_ROOT_PASSWORD" \
  -e "SELECT 'OK' AS db_ok, VERSION() AS version;"
brew services list | grep -E 'mariadb|redis'   # both must say "started"

# 2. Is the bench + site alive?
BENCH_DIR="$(grep ^BENCH_DIR= "$REPO_ROOT/.env" | cut -d= -f2)"
cd "$BENCH_DIR" && bench version                 # prints frappe + flock_os versions
bench --site "$SITE_NAME" list-apps              # flock_os must appear
bench --site "$SITE_NAME" execute frappe.get_installed_apps

# 3. Is the dev server responding?
curl -fsS "http://$SITE_NAME:8000/api/method/ping" || echo "web down"

# 4. Recently broken? Inspect logs (locations in [Log locations]).
tail -n 200 "$BENCH_DIR/logs/web.log"
tail -n 200 "$BENCH_DIR/logs/worker.log"     # queue workers (if running)
tail -n 200 "$BENCH_DIR/logs/schedule.log"

# 5. Most "weird behavior" bugs disappear on a clean cache + migrate.
bench --site "$SITE_NAME" clear-cache
bench --site "$SITE_NAME" migrate
```

If none of the above resolves it, go to [Troubleshooting](#troubleshooting) and
look up the specific symptom.

## Repository layout (mental model)

```
flock-os/                           # tracked repo == flock_os app source
  .env.example                      # copy to .env (NEVER commit .env)
  AGENTS.md                         # project-wide agent/human conventions
  README.md                         # mission + local-setup TL;DR
  flock_os/                         # the Frappe custom app package
    hooks.py                        # Frappe integration surface
    permissions.py                  # row-level scoping chokepoint (do not bypass)
    traversal.py                    # org-tree traversal service
    patches/                        # versioned data migrations (v0_1, v0_2, v0_3)
    flock_os/doctype/               # domain DocTypes
  scripts/
    bootstrap.sh                    # one-shot reproducible setup
    bootstrap-db.sh                 # MariaDB TCP-auth prep
    qa-gate.sh                      # local pre-merge gate (CI parity)
    dev/                            # backup/restore, ceo monitor, worktree helper, …
  docs/
    development/                    # dev workflow + planning docs
    operations/                     # this runbook + sibling ops runbooks
    security/                       # permission audits
  .github/workflows/ci.yml          # lint + unit-test gate

$BENCH_DIR/                         # Frappe runtime (OUTSIDE the repo)
  apps/                             # apps installed into the bench (flock_os → symlink to repo)
  sites/
    $SITE_NAME/                     # the actual Frappe site
      site_config.json              # per-site config incl. DB creds (SENSITIVE)
      private/files/ public/files/  # uploaded files (must be backed up)
  env/                              # the bench's Python venv
  logs/                             # web.log, worker.log, schedule.log, …
  backups/                          # backup archives land here
```

The bench tree is intentionally **outside the repo** so the tracked tree stays
small and so the editable install (`pip -e`) means edits in `flock-os/` are live
in the running site immediately.

## Core administration

### Prerequisites & first-time setup

Prerequisites on this Mac are already in place: Homebrew **MariaDB** + **Redis**
running as launchd services, **Python 3.12**, **Node** (via `mise`),
**frappe-bench**, and **uv**. If any are missing, install with:

```bash
brew install mariadb redis python@3.12 uv
brew install --HEAD frappe-bench        # or: pip install --user frappe-bench
mise use node@lts                       # node for asset building
```

The reproducible local setup is one command. It is idempotent — re-running it
never destroys data:

```bash
# 1. One-time: create local secrets (NEVER commit .env).
cp .env.example .env
#    fill MARIADB_ROOT_PASSWORD and SITE_ADMIN_PASSWORD with generated values, e.g.
python3 -c "import secrets; print(secrets.token_urlsafe(18))"

# 2. Reproducible setup: preps MariaDB, bench-inits Frappe v15 (py3.12),
#    installs the flock_os app, and creates the site. Re-runnable / idempotent.
./scripts/bootstrap.sh

# 3. Run the dev server (from the bench dir):
cd "$(grep ^BENCH_DIR= .env | cut -d= -f2)"
bench --site flock_os.localhost serve      # http://flock_os.localhost:8000
#    (add `flock_os.localhost` -> 127.0.0.1 to /etc/hosts for the web URL)
```

`bootstrap.sh` runs `bootstrap-db.sh` internally, which creates the
`frappe_root`@`%` TCP user Frappe needs. **Do not skip it** — without
`frappe_root`, `bench new-site` cannot connect (see
[Troubleshooting](#access-denied-for-user-frappe_root-or-root))).

The authoritative quick-start is also in [`README.md`](../../README.md); this
runbook is the operational companion.

### Starting and stopping services

MariaDB and Redis run as **Homebrew launchd services** (start on boot):

```bash
brew services list | grep -E 'mariadb|redis'      # status
brew services restart mariadb                       # restart DB
brew services restart redis                         # restart cache/queue/realtime
# If a service refuses to start, inspect its log:
tail -n 200 "$(brew --prefix)/var/log/mariadb.log"
tail -n 200 "$(brew --prefix)/var/log/redis.log"
```

The **dev web server** is a foreground process — run it in its own terminal or
under tmux:

```bash
cd "$BENCH_DIR"
bench --site "$SITE_NAME" serve                     # http://$SITE_NAME:8000
# Stop it with Ctrl-C. There is no `bench stop` — kill the process.
```

The async surface (queue workers, scheduler, realtime socketio) is **not**
started by `bench serve`. For local functional tests you usually don't need it.
If you do (e.g. testing realtime features):

```bash
cd "$BENCH_DIR"
bench --site "$SITE_NAME" worker                    # queue worker(s) — bg or tmux
bench --site "$SITE_NAME" schedule                  # cron-like scheduler
bench --site "$SITE_NAME" socketio                  # realtime tier
```

### Common site configuration

Per-site config lives in `sites/$SITE_NAME/site_config.json` (a JSON file
Frappe reads on every request). Mutate it through `bench` rather than editing
by hand where possible:

```bash
cd "$BENCH_DIR"

# Inspect the current config:
bench --site "$SITE_NAME" site-config               # never prints the db_password by default

# Common keys (set / get):
bench --site "$SITE_NAME" set-config maintenance_mode 1     # turn ON maintenance mode
bench --site "$SITE_NAME" set-config maintenance_mode 0     # turn it back off
bench --site "$SITE_NAME" set-config developer_mode 1       # useful while debugging
# Limit the bench to one site (avoid the "default site" prompt):
bench --site "$SITE_NAME" use "$SITE_NAME"
```

To change **global** bench behaviour (which site is default, http timeout,
etc.), edit `sites/common_site_config.json`. The DB password is in
`site_config.json` under `db_password` — that file is **secret** (it is
captured in every backup archive, see [Backup & restore](#backup--restore)).

### User management (Frappe)

> **Multi-tenant context.** Flock OS adds **row-level scoping** on top of
> Frappe's role/permission system. Frappe "the user can read this DocType" is
> necessary but not sufficient — the row-level scope (which branch / group
> subtree the user can see) is enforced by
> [`flock_os.permissions`](../../flock_os/permissions.py). Always go through
> `assert_branch_scope` / `assert_group_scope` /
> `resolve_leader_scope`; **never check perms via ad-hoc SQL**.

Create users and grant roles the standard Frappe way:

```bash
cd "$BENCH_DIR"

# Add a Frappe user (sends no email in local-only mode):
bench --site "$SITE_NAME" add-user alice@example.com \
  --first-name Alice --last-name Member --password "$(openssl rand -base64 18)"

# Grant / revoke a role:
bench --site "$SITE_NAME" set-role alice@example.com --role "Flock Branch Admin"
# (use the role name exactly as defined in the DocType's role_permissions)

# Disable a user (non-destructive; preserves their data + audit history):
bench --site "$SITE_NAME" execute "frappe.core.doctype.user.user.deactivate_user" \
  --args '["alice@example.com"]'
```

For non-trivial grants (bulk role assignment, scoped admins, group leaders), do
**not** script raw SQL — write a one-off Frappe console command:

```bash
bench --site "$SITE_NAME" console
# >>> import frappe
# >>> user = frappe.get_doc("User", "alice@example.com")
# >>> user.add_roles("Flock Group Leader")
# >>> user.save()
```

> **Flock OS domain users.** The app links Frappe users to domain people via
> `Flock Member` (`linked_user`, 1:1). When onboarding a real person, create
> both the Frappe user and the matching `Flock Member`, then set `linked_user`
> — otherwise permission scoping will not recognise them.

Permission audit / row-level scope diagnosis is documented separately in
[`docs/security/permission-audit.md`](../security/permission-audit.md).

### Backup & restore

Backups and restores have their **own dedicated runbook** at
[`docs/operations/backup-restore.md`](backup-restore.md). Read that before any
restore — restores are destructive and the scripts require explicit `--confirm`
(and `--force` over existing data).

The one-screen summary:

```bash
# Back up (archive under $BENCH_DIR/backups/<site>-<timestamp>/)
scripts/dev/backup.sh

# Restore into a target site (non-destructive flag required):
scripts/dev/restore.sh "<archive-dir>" --site recovery.localhost --confirm

# Full restore drill (backup → fresh drill site → row-count parity). Run before each release.
scripts/dev/restore-drill.sh
```

Operational notes worth remembering here:

- Archives **contain** `site_config.json` (DB creds). They must **never** be
  committed; `.gitignore` excludes the bench tree, but a manual copy outside it
  is your responsibility.
- The **restore drill** is the RTO confidence gate — run it before every
  release (acceptance criterion from [FLO-231](/FLO/issues/FLO-231)).
- Prod off-host backup automation lands with [FLO-246](/FLO/issues/FLO-246).
  Until then, every backup on this Mac is **local-only** — not a real backup.

### Migrations, patches, and fixtures

`flock_os` ships **versioned patches** under `flock_os/patches/v0_1`,
`v0_2`, `v0_3` (referenced by `flock_os/patches.txt`). Patches are
**idempotent data migrations** — they are how we add indexes, seed fixtures,
and backfill structural changes. Frappe records each executed patch in the
`__Patch` log so re-running `bench migrate` only applies new ones.

```bash
cd "$BENCH_DIR"

# Apply all pending migrations + patches (run after every `git pull`):
bench --site "$SITE_NAME" migrate

# Force-run a specific patch (rare; use only when debugging a stuck migration):
bench --site "$SITE_NAME" run-patch flock_os.patches.v0_3.add_group_member_indexes

# Re-export fixtures (rare; only if a fixture record changed in the UI):
bench --site "$SITE_NAME" export-fixtures --app flock_os
```

When to add a **new** patch:

- You added an index that must exist on existing installs (see
  `add_group_member_indexes.py` for the template).
- You need to backfill a default on existing rows.
- You are seeding new domain fixtures (mirror them in `flock_os.fixtures`
  **and** the `seed_*_fixtures.py` patch — `hooks.py` keeps the two in sync).

Pure schema changes belong in the DocType JSON, not a patch — Frappe applies
DocType schema changes automatically during `migrate`.

### Common maintenance tasks

```bash
cd "$BENCH_DIR"

# Clear every cache Frappe uses (redis cache, metadata, jinja, sessions):
bench --site "$SITE_NAME" clear-cache
# Clear-cache + clear-sessions (forces everyone to log in again):
bench --site "$SITE_NAME" clear-website-limits 2>/dev/null || true   # legacy flag, ignore errors
bench --site "$SITE_NAME" clear-cache --clear-sessions

# Rebuild search indices and assets:
bench --site "$SITE_NAME" clear-cache
bench build                               # rebuild JS/CSS assets

# Run the scheduler manually (debug cron-style jobs without waiting for cron):
bench --site "$SITE_NAME" scheduler                       # one shot
bench --site "$SITE_NAME" scheduler resume                # un-pause if paused

# Restart everything bench manages (workers, scheduler, web):
bench restart                              # only meaningful if bench start was used
```

If the site is wedged after a code change, the deterministic "turn it off and
on again" is: `clear-cache` → `migrate` → restart `bench serve`.

### Running the test gate

Two distinct test surfaces — do not confuse them:

```bash
# Fast project-level unit tests (no bench, no Frappe import).
# This is the CI gate and the merge gate (scripts/qa-gate.sh).
cd "$REPO_ROOT"
pip install ruff pytest && pip install -e . --no-deps
ruff check . && ruff format --check . && pytest

# Frappe integration tests (need a running bench + site).
cd "$BENCH_DIR"
bench --site "$SITE_NAME" run-tests --app flock_os
```

The merge gate enforces both: `scripts/qa-gate.sh` runs `[1/4]` ruff check,
`[2/4]` ruff format check, `[3/4]` foundation pytest, `[4/4]` coverage
ratchet, `[5/5]` operator-tooling pytest. Always run the gate inside a slice
worktree (see [Workspace isolation](#workspace-isolation)).

## Troubleshooting

### General approach

1. **Reproduce.** Run the failing command in a slice worktree so the working
   tree is clean (`scripts/dev/issue-worktree.sh create <ISSUE>`).
2. **Read the logs first.** 90% of issues announce themselves in
   `$BENCH_DIR/logs/` (see [Log locations](#log-locations)).
3. **Verify the platform services.** Redis down looks like a "random permission
   error"; MariaDB down looks like a "white page". Check both before deep-diving
   into Frappe.
4. **Cache + migrate** fixes the majority of post-pull weirdness.
5. If you change code, **run the gate** before declaring it fixed — the bug may
   have been introduced by an earlier "fix".

### `Access denied for user 'frappe_root'` (or 'root')

**Cause:** Frappe connects over TCP (`127.0.0.1:3306`), which requires a
password-authenticated root-equivalent user. Homebrew MariaDB enables
unix-socket auth, so the OS user is a passwordless superuser via the local
socket but TCP needs the dedicated `frappe_root` user.

**Fix:**

```bash
# 1. Confirm the password in .env matches the one MariaDB knows:
grep MARIADB_ROOT_PASSWORD "$REPO_ROOT/.env"

# 2. Re-run the one-time prep (idempotent; recreates frappe_root@% with that password):
"$REPO_ROOT/scripts/bootstrap-db.sh"

# 3. Verify TCP login explicitly:
mysql -u frappe_root -h 127.0.0.1 -P 3306 -p"$MARIADB_ROOT_PASSWORD" \
  -e "SELECT 'frappe_root@% TCP OK' AS status, VERSION() AS version;"
```

Why a dedicated user (`frappe_root`) and not `root`: Homebrew MariaDB ships
`root`@`localhost` which always shadows `root`@`%` on local TCP — a host-match
conflict that produces exactly this error. A dedicated name sidesteps it. See
the header comment in `scripts/bootstrap-db.sh` for the full rationale.

### `OperationalError: (2003, "Can't connect to MySQL server")`

- `brew services list | grep mariadb` shows `started`? If not:
  `brew services start mariadb`.
- `lsof -i :3306 | head` shows `mysqld`? If not, the service crashed — read
  `$(brew --prefix)/var/log/mariadb.log` for the cause.
- `$BENCH_DIR/sites/$SITE_NAME/site_config.json` `host` is `127.0.0.1` (not
  `localhost`) and `port` is `3306`.

### Site returns a blank page / 500 on every request

1. Tail `$BENCH_DIR/logs/web.log` — the traceback is there.
2. Most common cause after a `git pull`: a pending migration. Run
   `bench --site "$SITE_NAME" migrate`.
3. If a patch is failing, read the patch's source under
   `flock_os/patches/`, fix forward, and re-run migrate. Patches are
   idempotent — re-running never double-applies.
4. As a last resort, `bench --site "$SITE_NAME" clear-cache` then restart
   `bench serve`.

### `bench serve` says "Address already in use"

A previous `bench serve` is still bound to :8000.

```bash
lsof -i :8000 -t | xargs kill -9      # kill stale bench serve
# or pick a different port:
bench --site "$SITE_NAME" serve --port 8010
```

### Redis errors (`redis.exceptions.ConnectionError`, realtime not delivering)

```bash
redis-cli ping                       # expect PONG
brew services list | grep redis      # expect started
redis-cli info clients | head        # are connections leaking?
```

Redis backs three concerns for Flock OS: **cache**, **queue**, and
**realtime pub/sub**. A Redis blip tends to look like "everything is subtly
wrong" rather than a clean error. Restart it
(`brew services restart redis`) and run `bench --site "$SITE_NAME"
clear-cache` after.

Realtime-specific edge cases (auth cache TTL, room capacity, multi-branch
membership determinism, reconnect storms) are documented in
[`docs/operations/realtime-edge-cases.md`](realtime-edge-cases.md). Read that
before changing the realtime tier.

### Permission / scoping "user cannot see X they should"

> **Never patch this with raw SQL or a DocType change.**
> [`flock_os.permissions`](../../flock_os/permissions.py) is the **single
> chokepoint** for row-level scoping.

Diagnose:

```bash
cd "$BENCH_DIR"
bench --site "$SITE_NAME" console
# >>> import frappe
# >>> from flock_os.permissions import resolve_leader_scope, compute_branch_subtree
# >>> frappe.set_user("alice@example.com")
# >>> resolve_leader_scope()                      # what subtree can Alice see?
# >>> compute_branch_subtree("<branch-name>")     # is the branch visible?
# >>> from flock_os.permissions import assert_branch_scope
# >>> assert_branch_scope("<branch-name>")        # raises if she cannot
```

If the assertion fails for the right user, the issue is upstream — check the
`Flock Branch Admin Scope` / `Flock Group Member` records that materialise
their scope. The full permission audit procedure is in
[`docs/security/permission-audit.md`](../security/permission-audit.md).

### Slow queries / performance basics

```bash
# Turn on the MariaDB slow-query log temporarily:
mysql -u frappe_root -h 127.0.0.1 -P 3306 -p"$MARIADB_ROOT_PASSWORD" <<'SQL'
SET GLOBAL slow_query_log = 'ON';
SET GLOBAL long_query_time = 0.5;     -- log anything slower than 500ms
SET GLOBAL slow_query_log_file = '/tmp/flock-slow.log';
SQL

# Reproduce the load, then inspect:
tail -n 200 /tmp/flock-slow.log
# EXPLAIN any suspect query before changing indexes.
```

Two rules for fixes:

1. **Prefer a versioned patch.** Indexes on existing installs must land via
   `flock_os/patches/v0_X/...` (template: `add_group_member_indexes.py`). A
   DocType JSON `search_index` flag only sets a single-column index — composite
   hot-path indexes need a patch. This is exactly what the
   [FLO-454](/FLO/issues/FLO-454) 15k stress drill surfaced.
2. **The 15k-attendee scale is a design constraint.** If a path is in the
   registration / reporting hot loop, assume it will be hit at 15k concurrency.
   Measure, add a patch, re-measure — do not "optimise" without numbers.

For load testing the scale tier, see the `load/` directory's existing stress
harness.

### Migration fails partway

```bash
# Find the failing patch:
bench --site "$SITE_NAME" migrate 2>&1 | tee /tmp/migrate.log

# Check which patches Frappe thinks are pending:
bench --site "$SITE_NAME" execute "frappe.modules.utils.get_pending_patches" \
  2>/dev/null || tail -n 50 "$BENCH_DIR/logs/worker.log"
```

Common causes:

- A patch references a DocType field that does not exist yet — fix the patch
  order in `patches.txt`, or add the field via DocType JSON first.
- A patch errors mid-run and Frappe marks it failed — fix forward and re-run
  `migrate`. Patches must be **idempotent** so a partial run is recoverable.
- A DB lock from a long-running web request — stop `bench serve`, run migrate,
  restart.

### CI is red but local gate is green (or vice versa)

The local gate (`scripts/qa-gate.sh`) is built to mirror
`.github/workflows/ci.yml` exactly. The known parity traps are documented in
`docs/development/per-slice-worktrees.md` and inline in the gate:

- **Untracked `*.py` in the local tree** are silently skipped by the gate
  (CI parity: `actions/checkout` never sees them). `git add` new files
  **before** gating.
- **A tracked `.py` deleted mid-refactor** must not red the local gate (CI's
  checkout still has it). The gate skips missing files.
- The coverage ratchet measures only committed foundation source — it omits
  untracked WIP modules dynamically so an in-flight slice does not trip the
  floor.

If you see divergence, check whether one side has staged/unstaged files the
other doesn't.

### Worktree refuses to merge (`main tree has uncommitted changes`)

You edited `master` directly instead of using a slice worktree. Per
[FLO-91](/FLO/issues/FLO-91) (project-wide blocker), **all** code edits go in
a worktree. To recover: commit or stash the dirty `master`, then re-run
`scripts/dev/issue-worktree.sh merge <ISSUE>`. Going forward, always
`scripts/dev/issue-worktree.sh create <ISSUE>` first.

## Log locations

| Concern | File |
| --- | --- |
| Web / API requests, request-time tracebacks | `$BENCH_DIR/logs/web.log` |
| Background queue workers | `$BENCH_DIR/logs/worker.log` |
| Scheduled jobs | `$BENCH_DIR/logs/schedule.log` |
| Realtime socketio tier | `$BENCH_DIR/logs/realtime.log` (when `bench socketio` is running) |
| Frappe install / migrate / patches | `$BENCH_DIR/logs/install.log` |
| MariaDB server | `$(brew --prefix)/var/log/mariadb.log` |
| Redis server | `$(brew --prefix)/var/log/redis.log` |
| Per-process / per-worker crash dumps | `$BENCH_DIR/logs/*.log.<pid>` (rotated) |

```bash
# Live tail everything bench is writing:
tail -F "$BENCH_DIR/logs/"*.log
```

The Paperclip control plane (agent heartbeats, CEO heartbeat monitoring) has
its own logging outside this repo — see
[`docs/operations/ceo-heartbeat-monitoring.md`](ceo-heartbeat-monitoring.md)
and [`docs/operations/ceo-heartbeat-timeout.md`](ceo-heartbeat-timeout.md).

## Service health checks

```bash
# Fast, dependency-free smoke:
redis-cli ping                                # -> PONG
mysql -u frappe_root -h 127.0.0.1 -P 3306 \
  -p"$MARIADB_ROOT_PASSWORD" -e "SELECT 1;"   # -> 1

# Frappe-level smoke (from bench dir):
bench --site "$SITE_NAME" execute frappe.get_installed_apps
bench --site "$SITE_NAME" execute frappe.db.sql --args '["SELECT 1"]'

# HTTP smoke:
curl -fsS "http://$SITE_NAME:8000/api/method/ping"
```

**CEO heartbeat health** (the most important service in the company — a stuck
CEO is a single point of failure) has a dedicated monitor:

```bash
scripts/dev/ceo-heartbeat-monitor.py          # one-shot scrape
# Exit codes: 0 ok, 1 warning, 2 critical, 3 config
```

See [`docs/operations/ceo-heartbeat-monitoring.md`](ceo-heartbeat-monitoring.md)
for thresholds, alert routing, and the recovery path in
[`docs/operations/agent-liveness-recovery-runbook.md`](agent-liveness-recovery-runbook.md).

## Secrets handling

- **`.env` is gitignored and never committed.** It holds
  `MARIADB_ROOT_PASSWORD`, `SITE_ADMIN_PASSWORD`, and Paperclip monitor
  credentials. See `.env.example` for the template.
- **No secret belongs in agent prompts.** When a Paperclip run needs a secret,
  it is auto-injected via environment variables by the adapter; for standalone
  cron (e.g. the CEO monitor), provision a long-lived key and store it outside
  the repo.
- **Backup archives are secret.** They contain `site_config.json` with DB
  credentials. They live under `$BENCH_DIR/backups/` (outside the repo and
  gitignored) — never copy them into the tracked tree.
- **No secret belongs in code or in this runbook.** Every value below is a
  placeholder; substitute from `.env` or the secret manager.

## Workspace isolation

Every code edit, including ops scripts, happens in a **per-slice git worktree**
(project-wide blocker [FLO-91](/FLO/issues/FLO-91)). This is mandatory because
the merge gate cannot stay green when multiple heartbeats mutate the shared
`master` tree. Full workflow in
[`docs/development/per-slice-worktrees.md`](../development/per-slice-worktrees.md).

```bash
scripts/dev/issue-worktree.sh create FLO-530          # 1. provision
cd "$(scripts/dev/issue-worktree.sh path FLO-530)"    # 2. work here
# git add new files BEFORE gating (the gate scans tracked files only)
scripts/dev/issue-worktree.sh gate   FLO-530          # 3. merge gate in this tree
scripts/dev/issue-worktree.sh merge  FLO-530          # 4. merge into master (if green)
scripts/dev/issue-worktree.sh remove FLO-530          # 5. teardown
```

## Appendix — file & doc reference

### Key files (tracked repo)

| File | What |
| --- | --- |
| [`README.md`](../../README.md) | Mission + local-setup TL;DR. |
| [`AGENTS.md`](../../AGENTS.md) | Project-wide agent/human conventions, Frappe rules. |
| [`.env.example`](../../.env.example) | Template for the gitignored `.env`. |
| [`scripts/bootstrap.sh`](../../scripts/bootstrap.sh) | One-shot reproducible local setup. |
| [`scripts/bootstrap-db.sh`](../../scripts/bootstrap-db.sh) | MariaDB `frappe_root` TCP-auth prep. |
| [`scripts/qa-gate.sh`](../../scripts/qa-gate.sh) | Local pre-merge gate (CI parity). |
| [`scripts/dev/backup.sh`](../../scripts/dev/backup.sh) | Site backup → archive dir. |
| [`scripts/dev/restore.sh`](../../scripts/dev/restore.sh) | Restore from archive (`--confirm` required). |
| [`scripts/dev/restore-drill.sh`](../../scripts/dev/restore-drill.sh) | Backup → fresh drill site → parity check. |
| [`scripts/dev/issue-worktree.sh`](../../scripts/dev/issue-worktree.sh) | Slice worktree lifecycle helper. |
| [`scripts/dev/ceo-heartbeat-monitor.py`](../../scripts/dev/ceo-heartbeat-monitor.py) | CEO liveness monitor. |
| [`flock_os/hooks.py`](../../flock_os/hooks.py) | Frappe integration surface (events, fixtures, patches). |
| [`flock_os/permissions.py`](../../flock_os/permissions.py) | **Row-level scoping chokepoint — never bypass.** |
| [`flock_os/traversal.py`](../../flock_os/traversal.py) | Org-tree traversal service. |
| [`flock_os/patches/`](../../flock_os/patches) | Versioned data migrations (`v0_1`, `v0_2`, `v0_3`). |
| [`flock_os/patches.txt`](../../flock_os/patches.txt) | Ordered patch run-list. |

### Key files (bench tree, outside repo)

| File | What |
| --- | --- |
| `sites/$SITE_NAME/site_config.json` | Per-site config incl. `db_password` — **SENSITIVE**. |
| `sites/common_site_config.json` | Bench-global config (default site, http timeout). |
| `sites/$SITE_NAME/private/files/`, `public/files/` | Uploaded files — must be in every backup. |
| `apps/flock_os` | Symlink to the tracked repo (editable install). |
| `env/` | The bench's Python venv. |
| `logs/` | All Frappe logs. |
| `backups/` | Backup archives land here (sensitive). |

### Companion runbooks (do not duplicate)

- [Backup & restore](backup-restore.md) — backup strategy, restore drill, retention, RPO/RTO.
- [CEO heartbeat monitoring](ceo-heartbeat-monitoring.md) — detection + observability for the CEO silent-run defense-in-depth.
- [CEO heartbeat timeout](ceo-heartbeat-timeout.md) — platform-level run duration caps.
- [CEO recovery runbook](ceo-recovery-runbook.md) — CEO-specific recovery path (superseded for general agents by the liveness runbook below).
- [Agent liveness recovery](agent-liveness-recovery-runbook.md) — detect → terminate → restart → escalate, for any senior agent.
- [Realtime edge cases](realtime-edge-cases.md) — authorization edge-case audit for the 15k-attendee burst tier.
- [Per-slice worktrees](../development/per-slice-worktrees.md) — mandatory workspace isolation workflow.
- [Phase 2 gate protocol](../development/phase2-gate-protocol.md) — release/gate protocol for Phase 2 slices.
- [WebSocket broadcast delivery](../development/ws-broadcast-delivery.md) — realtime delivery design.
- [Permission audit](../security/permission-audit.md) — security/permission audit procedure.

### External documentation

- [Frappe Framework docs](https://frappeframework.com/docs) — the canonical
  reference for `bench`, DocTypes, permissions, REST, and the standard admin
  workflows. Read this first for any "how do I do X in Frappe" question.
- [Frappe Bench CLI](https://frappeframework.com/docs/user/en/bench)
  — `bench` command reference (`new-site`, `migrate`, `serve`, `run-tests`, …).
- [erpnext/frappe on GitHub](https://github.com/frappe/frappe) — source.
- [MariaDB documentation](https://mariadb.com/kb/en/documentation/) — SQL,
  tuning, and server administration.
- [Redis documentation](https://redis.io/docs/) — commands, persistence,
  clustering.

## Escalation path

Flock OS is operated by an **autonomous AI company on Paperclip**. The
escalation chain follows the chain of command — the golden rule is *never ask
a human to do what an agent could do*.

```
Issue / incident
   │
   ▼
DevOps Engineer (this runbook)         ── owns runtime, DB, queue, CI/CD, deploys
   │ if blocked or out-of-scope
   ▼
Software Architect                     ── owns system design + cross-cutting issues
   │ if blocked or out-of-scope
   ▼
CEO                                    ── 15-min heartbeat; sets strategy + priorities
   │ if blocked or out-of-scope (rare)
   ▼
Board                                  ── strategic / spend / company-level only
```

Concrete actions:

- **Code/domain issue** (not ops): assign the relevant issue to the
  Frontend / Backend / QA engineer via Paperclip rather than fixing it here.
- **Permission / scope bug**: route to the Software Architect — the
  permissions chokepoint is a system-design surface.
- **Agent stuck (any agent silent past its heartbeat budget)**: follow
  [`docs/operations/agent-liveness-recovery-runbook.md`](agent-liveness-recovery-runbook.md).
  The recovery owner is the **stuck agent's manager**, never the stuck agent
  itself (the [FLO-365](/FLO/issues/FLO-365) incident's lesson).
- **CEO silent**: the watchdog routine
  (`1cf1540e-17b5-47c5-b0c7-64fc69edd441`) detects → terminates → restarts;
  the Software Architect is the configured peer owner; the board is the
  escalation backstop. See
  [`docs/operations/ceo-heartbeat-monitoring.md`](ceo-heartbeat-monitoring.md).
- **Bleeding / data loss / suspected breach**: page the Software Architect
  immediately via a Paperclip issue with `priority: critical`; if the Architect
  is unresponsive within one heartbeat cycle, the CEO takes it; board-level
  only for actual company-level decisions (spend, public disclosure).

> **Why this shape.** Every agent heartbeat runs on the same budget. Asking a
> human to do something an agent could do wastes the company's most expensive
> resource — board attention. When in doubt, escalate **up the agent chain**
> first; the board sees only what genuinely requires board judgment.

## Change log

| Date | Issue | Change |
| --- | --- | --- |
| 2026-06-21 | [FLO-530](/FLO/issues/FLO-530) | Initial MVP operational runbook (core admin + troubleshooting). |
