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
flock-os/                       # this repo (Paperclip project workspace)
  AGENTS.md                     # project-wide instructions + conventions
  .paperclip/
    manifest.json               # the Paperclip company/org design (human-readable)
    agents/                     # per-role instruction files (fed to each agent)
```

The Frappe site + the `flock_os` custom app are scaffolded under this repo by the
agents (see the `flock_os/` app directory once Sprint 0 lands).

## Operating model

- Work is tracked as **issues** in Paperclip, traced back to the company goal.
- The **CEO** wakes every 15 minutes (heartbeat), reviews progress, delegates, and
  creates the next slice of work. Strategy changes go to the board for approval.
- Agents check out one task at a time (atomic), do the work, comment, and update
  status. Everything is auditable.
