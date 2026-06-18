# Flock OS

A **multi-branch organization / mega-church management SaaS** built on [Frappe](https://github.com/frappe/frappe).

Flock OS is built and operated by an **autonomous AI company** running on
[Paperclip](https://github.com/paperclipai/paperclip). Every employee is a coding
agent powered by **GLM-5.2** through a local [opencode](https://opencode.ai)
runtime (`opencode_local` adapter). The company runs a **15-minute CEO heartbeat**
that continuously drives the product forward in small iterative cycles.

## Mission

Give a large, geographically distributed organization (e.g. a mega church with
many branches across countries) one system to:

- Model the **org tree**: a root organization with many branches, each with its
  own admin team. Members join **groups**; groups nest into a tree (a member can
  lead one-to-many groups, which contain sub-groups, recursively).
- **Track gatherings/events** at every group level with time, date and attendees
  (including visitors not yet officially joined), reported by group leaders.
- **Schedule and announce** organization-level activities.
- Let admins **push notifications to leaders**, scoped precisely by org-tree node.
- Let attendees **self-register attendance in fun ways** — live mini-games and
  live questionnaires — instead of boring "mark present" forms. After a live game,
  the players are recorded as attendees.
- Support **one-time events** created by a group leader, approved up the tree by
  the relevant parent/branch leaders, then opened for registration with
  controllable scope.
- Scale a single event up to **~15,000 attendees**.

Enterprise-grade throughout: strong, flexible **permissions** (row/document/field
level, scoped by org-tree node), full audit trail, and a data-modeling-first
design approach.

## Tech stack

- **Frappe Framework** (`frappe`) as the base — DocTypes, permissions, REST, portals.
- **MariaDB** (primary DB) + **Redis** (cache, queues, realtime, pub/sub).
- A **Frappe custom app** (`flock_os`) on top of `frappe` for all domain logic.
- Additional Redis clustering if throughput requires it.

## Layout

```
flock-os/                       # this repo == the flock_os Frappe app source
  AGENTS.md                     # project-wide instructions + conventions
  setup.py                      # flock_os app packaging (this repo IS the app)
  flock_os/                     # the Frappe custom app package
    hooks.py                    # Frappe integration surface (events, fixtures, jobs)
    flock_os/                   # default module (DocTypes land here in FLO-3+)
    tests/                      # project-level unit tests (run in plain pytest)
  scripts/
    bootstrap.sh                # reproducible full local setup (bench + site + app)
    bootstrap-db.sh             # one-time MariaDB TCP-auth prep
  .github/workflows/ci.yml      # lint (ruff) + unit-test gate
  .env.example                  # copy to .env (gitignored) for local secrets
  .paperclip/
    manifest.json               # the Paperclip company/org design (human-readable)
    agents/                     # per-role instruction files (fed to each agent)
```

The Frappe **bench runtime** (`apps/`, `sites/`, `env/`, built assets) lives
**outside** the repo at `$BENCH_DIR` (see `.env`), so the tracked tree holds
only the app source + CI + scripts. The `flock_os` app is installed into the
bench as an editable (`pip -e`) package from this repo, so edits here are live.

## Phase 1 spine — org tree + permissions

Phase 1 ships the **org-tree + permission spine** every later feature builds on.
It is verified green by the QA gate ([FLO-21](/FLO/issues/FLO-21)) and signed
off as a unit at the [FLO-52](/FLO/issues/FLO-52) exit gate.

**Domain DocTypes** (`flock_os/flock_os/doctype/`):

- `Flock Organization` — the root org (tenant root).
- `Flock Branch` (`is_tree=1`) — the administrative org tree / branch axis.
- `Flock Group` (`is_tree=1`) — the ministry/cell tree, **branch-bound**
  (a group subtree lives within exactly one branch).
- `Flock Group Member` — the leadership roster / membership edge.
- `Flock Member` — a person (linked 1:1 to a Frappe User via `linked_user`).
- `Flock Branch Admin Scope` — materializes a Branch Admin's subtree.
- `Flock Audit Log` — full audit trail (every cross-scope read is recorded).

**Traversal + permission entry points** (domain logic, Frappe-agnostic):

- `flock_os.traversal` — `TreeTraversalService`: `branch_subtree`,
  `branch_descendants`, `group_subtree`, `branch_path_to_root`, the
  "member leads 1..N groups" lookups, and the approval-routing leader chain.
  Exposed at REST via `@frappe.whitelist()` endpoints (`get_branch_subtree`,
  `get_group_descendants`, …).
- `flock_os.permissions` — the single row-level-scoping chokepoint:
  `assert_branch_scope` / `assert_group_scope` (guards),
  `resolve_leader_scope`, `get_group_scoped_conditions` (the one
  `permission_query_conditions` hook), and `compute_branch_subtree`.

Both services are hexagonal: pure domain logic over a gateway port, with the
Frappe adapter imported lazily so the full suite runs under plain `pytest`
without a bench.

## Local setup runbook (macOS / Homebrew)

Prerequisites (already on this Mac): Homebrew **MariaDB** + **Redis** running as
launchd services, **Python 3.12** (`python@3.12`), **Node** via `mise`,
**frappe-bench**, and **uv** (`brew install uv`).

```bash
# 1. One-time: create local secrets (NEVER committed — .env is gitignored).
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

### Tests

```bash
# Fast project-level unit tests (no bench/Frappe needed) — this is the CI gate:
pip install ruff pytest && pip install -e . --no-deps
ruff check . && ruff format --check . && pytest

# Frappe-level integration tests (need a running bench + site):
cd "$BENCH_DIR" && bench --site flock_os.localhost run-tests --app flock_os
```

### Phase-1 spine demo (FLO-52 sign-off)

A reproducible demo of the org-tree + permission spine — drives the **real**
`TreeTraversalService` and the permissions scoping API over an in-memory
multi-branch world (no Frappe bench needed), asserting all four DoD #5
guarantees (Branch Admin isolation, Group Leader scoping, Org Admin full tree,
Auditor read-only across branches). Exits non-zero if any guarantee fails:

```bash
./scripts/demo-phase1.sh        # or: python scripts/demo_phase1.py
```

The demo is also exercised by the unit gate (`test_demo_phase1.py`), so it
cannot rot behind a merge.

### CI

`.github/workflows/ci.yml` runs on every push/PR: installs the app package
(`--no-deps`), runs **ruff lint + format check**, then **pytest**. The unit
suite asserts app identity/metadata and does not import Frappe, so the gate is
fast and self-contained. Frappe DocType integration tests run locally via
`bench run-tests` (and will join CI once a headless Frappe image is wired up).

## Operating model

- Work is tracked as **issues** in Paperclip, traced back to the company goal.
- The **CEO** wakes every 15 minutes (heartbeat), reviews progress, delegates, and
  creates the next slice of work. Strategy changes go to the board for approval.
- Agents check out one task at a time (atomic), do the work, comment, and update
  status. Everything is auditable.
