# Flock OS — User Documentation

Product documentation for **end-users** (members, visitors) and **branch
admins / group leaders**. This is your single entry point for using Flock OS.

For engineering, ops, and security docs, see the sections further down.

---

## Start here

| Who you are | Read this |
|-------------|-----------|
| **Branch admin / group leader** (running events, taking attendance, sending announcements) | [Branch Admin Guide](./admin-guide.md) |
| **Member / visitor** (registering, joining sessions, event day) | [Attendee Guide](./attendee-guide.md) |

### What Flock OS does

Flock OS is a multi-branch organization management platform (built on Frappe).
It lets a large, geographically distributed organization:

- Model the **org tree** — a root organization with branches, each with its own
  admin team. Members join **groups**; groups nest into a tree within a branch.
- **Track gatherings/events** at every group level with attendance (including
  visitors), reported by group leaders.
- **Schedule and announce** organization-level activities.
- Let admins **push scoped announcements** to leaders/members.
- Let attendees **self-register attendance in fun ways** — live mini-games and
  live questionnaires — so attendance is engaging instead of a chore.
- Support **one-time events** created by a leader, approved up the tree, then
  opened for scoped registration.
- Scale a single event up to **~15,000 attendees**.

---

## Core concepts (30-second orientation)

- **`Flock Organization`** — your org (one per site). The tenant root.
- **`Flock Branch`** — a regional/campus node in the admin tree. Branches nest
  (`parent_branch`). A branch admin owns a subtree of branches.
- **`Flock Group`** — a ministry/cell unit. Groups nest (`parent_group`) and are
  bound to exactly one branch.
- **`Flock Member`** — a person. `status` is `Member`, `Pre-Member`, or
  `Visitor` (visitors are members with a status, not a separate type).
- **`Flock Gathering`** — an event. `Routine` (recurring) or `One-time`
  (approval-gated). Lifecycle: `Scheduled → Held → Reported → Confirmed`.
- **Fun Attendance** — a `Flock Engagement Session` (live game or questionnaire)
  at `/engage` (players) / `/engage-host` (facilitators). Playing it = attendance.
- **Roles** — `Flock Org Admin`, `Flock Branch Admin`, `Flock Group Leader`,
  `Flock Member`, `Flock Visitor`, `Flock Auditor`.

For the full canonical data model, see the Phase 1 design issue:
[FLO-5](/FLO/issues/FLO-5).

---

## User-facing portal pages

| Page | Who | Purpose |
|------|-----|---------|
| `/engage` | Members, visitors | Join a fun attendance session (link, room code, or QR). |
| `/engage-host` | Group leaders, branch admins | Facilitator console: create, open, close engagement sessions. |
| `/engage-templates` | Group leaders, branch admins | Manage reusable game / questionnaire templates. |
| `/announce` | Group leaders, branch admins | Compose + publish scoped announcements. |
| Desk (`/app`) | All admin roles | DocType management (branches, groups, members, events, etc.). |

---

## Related documentation

### Operations runbooks

These cover **how to run and operate** Flock OS in production (infrastructure,
deploy, incident response). Useful for the ops/DevOps team and on-call admins.

| Document | Purpose |
|----------|---------|
| [Backup & Restore Runbook](../operations/backup-restore.md) | Backup, restore, and the row-count-parity restore drill. |
| [Incident Runbooks](../operations/incident-runbooks.md) | Triage + stabilize procedures for six named incidents (WS storm, Redis failover, MariaDB deadlock, 15k degradation, deploy rollback, secret rotation). |
| [Launch Go / No-Go](../operations/launch-go-no-go.md) | Launch readiness signal catalog and the sign-off gate. |
| [Migration Runbook](../operations/migration-runbook.md) | Production `bench migrate` flow + rollback path. |
| [Scale @ 15k Findings](../operations/scale-15k-findings.md) | Data-tier stress findings and the performance backlog. |
| [Metrics & Alerting Design](../operations/metrics-alerting-design.md) | The canonical metric catalog, dashboard specs, and alert thresholds. |
| [Dashboards](../operations/dashboards/README.md) | Importable Grafana JSON templates for the ops boards. |

> **Note:** an event-day-specific runbook is referenced elsewhere in the project
> (attributed to FLO-581) but has not yet landed in the repository. Until it
> does, event-day operational guidance lives in
> [Incident Runbooks](../operations/incident-runbooks.md) and the
> [Launch Go / No-Go](../operations/launch-go-no-go.md) gate.

### Development docs

These cover **how to build and deploy** Flock OS (engineering audience).

| Document | Purpose |
|----------|---------|
| [Deploy Pipeline](../development/deploy-pipeline.md) | CI/CD job graph, environment/secret wiring, promotion gate. |
| [Deploy Runbook](../development/deploy-runbook.md) | Day-2 deploy + rollback operations. |
| [Provision Staging VM](../development/provision-staging-vm.md) | One-time staging VM bring-up. |
| [Staging Preflight Checklist](../development/staging-preflight-checklist.md) | Command-by-command preflight for staging. |
| [Secrets Runbook](../development/secrets-runbook.md) | SOPS + age secrets management. |
| [WebSocket Broadcast Delivery](../development/ws-broadcast-delivery.md) | How realtime broadcasts reach clients at scale. |
| [Per-Slice Worktrees](../development/per-slice-worktrees.md) | The mandatory isolated-worktree workflow. |
| [Phase 2 Gate Protocol](../development/phase2-gate-protocol.md) | CEO exec-acceptance protocol for review gates. |
| [FLO-454 Stress Findings](../development/flo-454-stress-findings.md) | Local 15k stress drill findings + re-run. |

### Security docs

| Document | Purpose |
|----------|---------|
| [Permission Audit](../security/permission-audit.md) | Full role × DocType × position permission matrix; severity-rated findings. |
| [Pre-Production Audit](../security/pre-production-audit.md) | OWASP-aligned static security review and posture. |

---

## Need more help?

- **Data model authority:** [FLO-5](/FLO/issues/FLO-5) (canonical model issue).
- **Permission model:** [Permission Audit](../security/permission-audit.md).
- **Running an event:** [Branch Admin Guide](./admin-guide.md) §2–§5.
- **Attending an event:** [Attendee Guide](./attendee-guide.md).
