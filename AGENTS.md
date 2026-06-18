# AGENTS.md — Flock OS

> Project-wide instructions for every agent (and human) working in this repo.
> Per-role behavior lives in `.paperclip/agents/<role>.md`; Paperclip injects the
> relevant one into each agent's run. The company **goal + the assigned issue +
> its ancestor chain** are also injected at runtime by Paperclip, so you always
> know *what* to do and *why*.

## What we are building

**Flock OS** — a multi-branch organization / mega-church management SaaS on
[Frappe](https://github.com/frappe/frappe). See `README.md` for the full mission.
Read it before doing anything in the domain layer.

### Non-negotiable product requirements

1. **Org tree**: root org → many branches (possibly across countries). Each branch
   has its own admin team. Members join **groups**; groups form a **tree** (a
   member may lead 1..N groups; groups nest recursively).
2. **Event/gathering tracking** at every group level: time, date, attendees
   (including visitors/pre-members), reported by the group leader.
3. **Org-level activity scheduling + announcements**.
4. **Admin → leader push notifications**, scoped by org-tree node (and subtree).
5. **Fun attendance**: live mini-games + live questionnaires that record the
   players as attendees. Make presence delightful, not bureaucratic.
6. **One-time events**: created by a group leader, approved up the reporting tree
   by the scoped branch leaders, then opened for scoped registration.
7. **Scale**: a single event may host up to **~15,000 attendees** — model indexes,
   bulk-write paths, and queue-based reporting accordingly.
8. **Enterprise permissions**: flexible, granular control (role, doc-level
   permissions, field-level, and row-level / org-tree-node scoping). Design
   permissions as a first-class concern, not an afterthought.

## Engineering principles (everyone follows these)

- **Data-modeling first.** Before writing a feature, model the entities and their
  relationships. Post the proposed DocTypes/fields/links in the issue's `design`
  document for review. The Product Manager owns the canonical data model.
- **DRY.** No duplicated logic. Shared behavior lives in reusable Frappe
  DocTypes, server scripts, whitelist utilities, or custom DocField hooks.
- **Event-modeling.** Think in events. State changes emit domain events
  (Frappe hooks + Redis pub/sub). Downstream features subscribe to events rather
  than polling or re-querying. Document the event catalog.
- **Separation of concerns.** Domain logic is independent of UI and transport.
  UI calls REST/websocket; REST calls services; services emit events.
- **Composable & extendable.** Prefer customization hooks, virtual DocTypes, and
  convention over hard-coded branches. Other branches/orgs will extend this.
- **SQL-light, project-level test coverage.** Write SQLite-fast, project-level
  tests (Frappe test records + unit tests) for every change. No PR merges without
  tests. The QA Engineer enforces coverage.
- **Small, reviewable changes.** One concern per issue. Update the issue with a
  short comment when done; move to `in_review`.

## Frappe conventions

- The custom app is `flock_os` (installed on a Frappe site). Domain DocTypes live
  in `flock_os/flock_os/doctype/`.
- Use Frappe's permission system: `role_permissions`, `if_owner`, field-level
  perms, and **row-level permission rules** scoped by org-tree node for tenant
  isolation across branches.
- Use **Frappe hooks** (`hooks.py`) for event emission; use **Redis pub/sub** +
  Frappe realtime for live features (games, questionnaires, notifications).
- REST: expose domain actions via `@frappe.whitelist()` and the standard
  `frappe.client.*` + resource endpoints. Bulk endpoints for 15k-scale writes.
- Migrations/fixtures via Frappe fixtures + versioned patches
  (`flock_os/patches`).
- Keep the `bench` dev site running locally (MariaDB + Redis via Homebrew).

## How we work (Paperclip heartbeat protocol)

On each heartbeat you will: read your identity (`GET /api/agents/me`), review your
assigned issues, **atomically check out** one (`POST /api/issues/{id}/checkout`),
do the work in this repo, comment on the issue with what you did, then update
status (`PATCH /api/issues/{id}`). Never retry a `409` (another agent owns it).
Escalate blockers by `@`-mentioning your manager or opening a child issue. Keep
the company goal in mind — every task traces back to it.

## Repo status

**Phase 1 (org-tree + permission spine) is shipped and QA-green** — verified by
the QA gate ([FLO-21](/FLO/issues/FLO-21)) and signed off at the
[FLO-52](/FLO/issues/FLO-52) exit gate. The spine every later feature builds on:

- **DocTypes** in `flock_os/flock_os/doctype/`: `Flock Organization`,
  `Flock Branch` (tree), `Flock Group` (tree, branch-bound), `Flock Group Member`,
  `Flock Member`, `Flock Branch Admin Scope`, `Flock Audit Log`.
- **Traversal** — `flock_os.traversal.TreeTraversalService`
  (`branch_subtree` / `group_subtree` / `branch_path_to_root` / leader chain) +
  `@frappe.whitelist()` REST endpoints.
- **Permissions** — `flock_os.permissions`: the single row-level-scoping
  chokepoint (`assert_branch_scope` / `assert_group_scope` /
  `resolve_leader_scope` / `get_group_scoped_conditions` /
  `compute_branch_subtree`). Never check perms via ad-hoc SQL — go through these.
- **Demo** — `./scripts/demo-phase1.sh` reproduces the spine's isolation
  guarantees over an in-memory multi-branch world (no bench needed).

Both services are hexagonal: pure domain logic over a gateway port, Frappe
adapter imported lazily, so the unit gate runs under plain `pytest`. Phase 2
features extend the spine — append transactional DocTypes to
`permissions.SCOPED_DOCTYPES` and reuse the traversal/permission entry points
rather than re-implementing scoping.

Sprint 0 (environment + data model) was bootstrapped by the agents. If
`bench`/the Frappe site is not yet initialized, the DevOps Engineer + Architect
own setting it up (see the seeded Sprint 0 issues).
