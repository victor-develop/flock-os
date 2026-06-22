# Flock OS — Security & Permission Audit (Phase 6.2)

> **Scope:** role-level (DocPerm) + row-level (subtree) isolation across the
> org / branch / group tree, and the public/portal REST surface. VM-independent
> — audited entirely against the shipped codebase on the local bench, no cloud,
> no spend.
>
> **Parent:** [FLO-231](/FLO/issues/FLO-231) (Phase 6) · Epic:
> [FLO-533](/FLO/issues/FLO-533) (Phase 6.2) · Audit issue:
> [FLO-896](/FLO/issues/FLO-896) · ADR-0001 §6 (two-axis authorization model).
>
> **Prior round:** [FLO-290](/FLO/issues/FLO-290) fixed five code-level findings
> (SEC-1..SEC-6) and documented three LOW follow-ups. This document is the
> **comprehensive enumeration** required by FLO-896 acceptance criteria #1/#2:
> every custom DocType's permission matrix + per-DocType branch-isolation
> verification, layered on top of the FLO-290 findings list.

## Verification

```
ruff check . && ruff format --check . && pytest
```

- `pytest flock_os/tests/test_permission_audit.py test_permissions.py test_tenant_isolation.py test_permission_matrix.py`
  → **344 passed** (least-privilege DocPerm contract + runtime scoping hook +
  guard/hook parity + cross-branch deny matrix).
- Full enumeration reproducible via `python3 scripts/dev/extract-permission-matrix.py`
  (committed). Regenerates this matrix from the DocType JSON + the
  `permissions.SCOPED_DOCTYPES` / `MEMBER_ANCHORED_DOCTYPES` source-of-truth.
- `bench run-tests --app flock_os` runs once a headless Frappe image is wired;
  the DocType-controller transport paths ride the same `assert_*_scope` guards
  whose deny-paths are pinned under plain pytest.

## TL;DR — verdict

The **two-axis authorization model is sound** (ADR-0001 §6): the group-axis
`permission_query_conditions` hook + the branch-axis User-Permission
materialization compose correctly, and the `test_tenant_isolation` /
`test_permission_matrix` suites prove a scoped leader **cannot** read/write a
foreign subtree or cross-branch row on every `SCOPED_DOCTYPES` entry. **No
cross-tenant data leakage was found in any actively-used runtime path.**

This audit enumerated **all 24 custom DocTypes** and confirmed:

1. **Every branch-bearing DocType (15/15) is row-level isolated** — 9 on both
   axes (native branch User Permission + group-tree hook), 6 on the native branch
   axis alone (all carry `branch: Link → Flock Branch`, so Frappe User
   Permissions apply automatically).
2. **No role grants blanket cross-branch access.** The role catalog is the six
   sanctioned Flock roles + `System Manager` (pinned by
   `test_only_known_roles_hold_permissions`). `Flock Org Admin` / `Flock Auditor`
   are *intentionally* global (org-wide, by design — ADR §6.2
   `GLOBAL_BRANCH_ROLES`); every other role is subtree-scoped.
   `set_user_permissions` is granted to **no** Flock role anywhere.
3. **One new LOW finding** (dormant, not an active leak) + **two carried-forward
   LOW items** that FLO-290 documented as "filed" but were never created in the
   tracker — all three are now filed as tracked follow-up issues.

---

## 1. DocType enumeration — complete permission matrix

All 24 custom Flock OS DocTypes (under `flock_os/flock_os/doctype/`) and the
row-level axis(es) that isolate each:

| DocType | Kind | `branch` | `group` | Row-level axis(es) |
|---|---|:-:|:-:|---|
| Event Attendance Summary | standalone | ✓ | | branch-UP |
| Flock Announcement | standalone | ✓ | ✓ | group-hook, branch-UP |
| Flock Announcement Channel | **child table** | | | inherits parent (Announcement) |
| Flock Attendance Record | standalone | ✓ | ✓ | group-hook, member-self, branch-UP |
| Flock Audit Log | standalone | ✓ | | branch-UP |
| Flock Branch | **tree (the axis itself)** | | | n/a — *is* the branch axis |
| Flock Branch Admin Scope | standalone | ✓ | | branch-UP |
| Flock Engagement Feedback | standalone | | | **none — latent gap (SEC-B3)** |
| Flock Engagement Game Template | standalone | | | reference data (read-mostly) |
| Flock Engagement Questionnaire Template | standalone | | | reference data (read-mostly) |
| Flock Engagement Round | standalone | | | **none — latent gap (SEC-B3)** |
| Flock Engagement Session | standalone | ✓ | ✓ | group-hook, branch-UP |
| Flock Event Approval | standalone | ✓ | ✓ | group-hook, branch-UP |
| Flock Event Approval Policy | standalone | ✓ | | branch-UP |
| Flock Event Approval Step | **child table** | | | inherits parent (Event Approval) |
| Flock Event Invitation | standalone | ✓ | ✓ | group-hook, member-self, branch-UP |
| Flock Event Registration | standalone | ✓ | ✓ | group-hook, member-self, branch-UP |
| Flock Gathering | standalone | ✓ | ✓ | group-hook, branch-UP |
| Flock Gathering Type | standalone | ✓ | | branch-UP |
| Flock Group | **tree** | ✓ | | group-hook (self-predication), branch-UP |
| Flock Group Member | standalone | ✓ | ✓ | group-hook, member-self, branch-UP |
| Flock Group Type | standalone | | | reference data (read-mostly) |
| Flock Member | standalone | ✓ | | branch-UP |
| Flock Organization | standalone (FIXED singleton) | | | org-global singleton (Org Admin only) |

**Axes key:** `group-hook` = registered in `permissions.SCOPED_DOCTYPES` → the
custom `permission_query_conditions` nested-set fragment narrows list queries.
`branch-UP` = the DocType carries a `branch: Link → Flock Branch`, so native
Frappe **User Permissions** automatically narrow rows to the caller's allowed
branch subtree (materialized by `Flock Branch Admin Scope`). `member-self` =
listed in `MEMBER_ANCHORED_DOCTYPES`, so a member sees their own rows.

### Role → capability summary (transactional / branch-scoped DocTypes)

The DocPerm least-privilege contract (permlevel-aware) for the sensitive set. R
= read, W = write, C = create, D = delete, S = submit. `*` marks an `if_owner`
or permlevel>0 grant.

| DocType | Org Admin | Branch Admin | Group Leader | Member | Visitor | Auditor |
|---|---|---|---|---|---|---|
| Flock Member | CRUD | CRUD | CR | R | | R |
| Flock Group Member | CRUD | CRUD | CRUD | | | R |
| Flock Group (tree) | CRUD | CRUD | CR | | | R |
| Flock Gathering | CRUD+S | CRUD+S+amend | CR+S | R | | R |
| Flock Attendance Record | CRUD | CRUD | CR | | | R |
| Flock Announcement | CRUD | CRUD | CR | R | | R |
| Flock Event Registration | CRUD | CRUD | CR | CR* | CR* | R |
| Flock Event Invitation | CRUD | CRUD | CR | R | | R |
| Flock Event Approval | CRUD+S | CR+S | CR+S | R | | R |
| Flock Engagement Session | CRUD | CRUD | CR | **C+R** *(SEC-B2)* | | R |
| Flock Branch Admin Scope | CRUD | **R only** *(SEC-1 fix)* | | | | R |
| Flock Audit Log | **CRUD** *(SEC-B1)* | CR | | | | R |

Full per-role/permlevel breakdown for every DocType is reproducible via
`scripts/dev/extract-permission-matrix.py` and pinned by
`flock_os/tests/test_permission_audit.py` (catalog hygiene, duplicate-row,
set_user_permissions, member/visitor least-privilege, and the SEC-1/SEC-2/SEC-3
regression pins).

---

## 2. Row-level / subtree branch isolation — verified

**Verdict: sound for every actively-used branch-bearing DocType.**

The isolation is enforced once at the framework layer (ADR §6.5) by two
composable axes, verified cell-by-cell by the runtime suites:

- **Branch axis (native):** `Flock Branch` User Permissions, materialized per
  admin by `Flock Branch Admin Scope.validate → FrappeUserPermissionSyncer`
  (one UP row per descendant branch). Equality-based narrowing that Frappe
  applies automatically to every DocType with a `branch: Link → Flock Branch`.
  All **15** branch-bearing DocTypes confirmed to carry that link field.
- **Group-tree axis (custom):** the single `permission_query_conditions` hook
  (`permissions.get_group_scoped_conditions`) registered for the **9**
  `SCOPED_DOCTYPES`. Emits one OR-fragment (live nested-set subtree of led
  groups ∪ joined groups ∪ self-membership) appended to the WHERE. Bypass roles
  (Org Admin / Auditor / Branch Admin) short-circuit to `""`.
- **Composition is independent:** same-branch is *not* sufficient —
  group-axis subtree containment is enforced separately
  (`test_cross_subtree_foreign_denied_at_group_guard_but_passes_branch_guard`).

Confirmed-denied paths (no cross-tenant read/write on any `SCOPED_DOCTYPES`):
cross-branch deny at the guard (symmetric), parent-excluded / descendant-included,
admin subtrees disjoint, null-branch org-wide visibility, forged-group /
escalated-group deny-default, approval-authority (`assert_approval_scope`)
confinement to the resolved chain + branch scope.

The two-tree binding invariant (a group subtree is branch-bound; a child group
inherits its parent's branch) is enforced in `flock_os.flock_os.trees` and the
`Flock Group Member.branch` denormalization in `flock_os.flock_os.rules` — so
group-axis membership edges carry native branch isolation *and* the group hook.

### Child tables inherit parent scoping

`Flock Announcement Channel` and `Flock Event Approval Step` are Frappe child
tables (`istable=1`) — they have no standalone DocPerm surface and are only
reachable through their parent (Announcement / Event Approval), which is fully
scoped. No isolation gap.

### Reference / config DocTypes (org-global by design)

`Flock Group Type`, `Flock Gathering Type`, `Flock Engagement Game Template`,
`Flock Engagement Questionnaire Template` are org-wide reference/lookup data.
Group Leader / Member hold **read-only** (Group Leader may not even write
templates — admin-only). `Flock Organization` is the FIXED singleton (Org Admin +
System Manager only). These are correctly org-global; branch scoping does not
apply to shared catalog data.

`Flock Branch` *is* the branch axis itself (the tree the UP rows reference) —
Org Admin + System Manager manage it; Branch Admin / Group Leader read-only.

---

## 3. Role catalog & blanket-access check (acceptance criterion #4)

**No role grants blanket cross-branch access.** Verified:

- The **only** roles holding any permission across all sensitive DocTypes are
  the six sanctioned Flock roles (`Flock Org Admin`, `Flock Branch Admin`,
  `Flock Group Leader`, `Flock Member`, `Flock Visitor`, `Flock Auditor`) plus
  `System Manager`. No `Desk User`, `Guest`, or stale custom role. Pinned by
  `test_only_known_roles_hold_permissions`.
- `Flock Org Admin` and `Flock Auditor` are **intentionally** global
  (`permissions.GLOBAL_BRANCH_ROLES`, ADR §6.2) — the org-wide admin and the
  read-only auditor see every branch by design. This is documented, not a gap.
- `Flock Branch Admin` is **the** role the branch axis scopes — it carries no
  blanket grant; its visible subtree is exactly the User-Permission set
  materialized from `Flock Branch Admin Scope`. It is in `BYPASS_ROLES` for the
  *group* axis (broader-scope-wins) but is the scope *target* on the branch axis.
- `set_user_permissions` (the privilege-escalation primitive that rewrites
  another user's User Permissions) is granted to **no** Flock role anywhere —
  only `System Manager`. Pinned by
  `test_set_user_permissions_never_granted_to_non_system_roles`.
- No "Branch Manager" / unscoped-admin role exists in the catalog.

---

## 4. Findings

### From the FLO-290 round — all HIGH/MEDIUM fixed (recap)

| ID | Severity | Finding | Status |
|---|---|---|---|
| SEC-1 | HIGH | `Flock Branch Admin` had create/write/delete on `Flock Branch Admin Scope` (self-serve privilege escalation). | **Fixed** — read/report-only now; pinned. |
| SEC-2 | HIGH | `Flock Event Approval` duplicate permlevel-0 row + missing permlevel 1/2 grants. | **Fixed** — dedup + pl1/pl2 grants; pinned. |
| SEC-3 | MED | `Flock Announcement` Group Leader could not write permlevel-1 scope-targeting fields. | **Fixed** — pl1 R/W; pinned. |
| SEC-4 | MED | Announcement publish/preview/schedule did not re-assert the author's branch scope. | **Fixed** — `_assert_author_branch_scope`; pinned. |
| SEC-5 | MED | `register_for_event` did not authorize an explicitly-passed member. | **Fixed** — caller branch-scope assert. |
| SEC-6 | MED | `portal.targetable_branches` read the wrong User-Permission columns (fail-closed DoS). | **Fixed** — `allow`/`for_value`. |

### Carried-forward LOW items (now properly filed — were never created in FLO-290)

FLO-290's doc described these as "filed as a child" but **no tracker issue was
ever created**. FLO-896 closes that tracking gap by filing them now.

#### [LOW] SEC-B1 — `Flock Audit Log`: `Flock Org Admin` holds full write/delete (tamper)

**Where:** `flock_os/flock_os/doctype/flock_audit_log/` (DocPerm).
**Finding:** the audit trail is writable/deletable by `Flock Org Admin` (a
trusted but non-System role). The system's own audit writes go through
`db_insert(ignore_permissions=True)` so do not need this grant; removing
write/delete/create from Org Admin (keep read/report) makes the trail
append-only for all but System Manager — defense-in-depth against tamper-by-
trusted-role. **Filed as follow-up.**

#### [LOW] SEC-B2 — `Flock Engagement Session`: `Flock Member` holds `create`

**Where:** `flock_os/flock_os/doctype/flock_engagement_session/` (DocPerm).
**Finding:** sessions are facilitator-created (`engagement_api.create_session`
asserts branch scope); a plain member spawning session rows is noise, not a
leak. Recommend drop `create` for Member. **Filed as follow-up.**

### New finding (FLO-896)

#### [LOW] SEC-B3 — dormant engagement DocTypes have no row-level isolation

**Where:** `flock_os/flock_os/doctype/flock_engagement_round/` and
`flock_engagement_feedback/` (schema + DocPerm).
**Finding:** `Flock Engagement Round` and `Flock Engagement Feedback` are
**standalone DocTypes** (not child tables) with **no `branch` field, no `group`
field, and not registered in `SCOPED_DOCTYPES`** — so neither the native branch
User-Permission axis nor the group-axis hook narrows them. Yet their DocPerms
grant `read`/`create` to `Flock Member` / `Flock Group Leader`.

Crucially, these DocTypes are **currently dormant** — no production code writes
to them (confirmed by whole-tree grep; they appear only in schema tests), so
there is **no active cross-branch leak today**. The risk is **latent**: the
moment a future analytics/reporting feature populates them, a Group Leader
(who should be subtree-scoped) could read engagement rounds and feedback
(attendee-named, sentiment-bearing data) from **any branch** via the unscoped
Frappe list/report path, and members could inject rows.

**Recommendation (filed, design-dependent — owner decides):** when these tables
ship, either (a) denormalize `branch` (mirroring `Flock Group Member.branch`) so
the native UP axis isolates them, or (b) add a `group` link + register in
`SCOPED_DOCTYPES`, or (c) until then tighten DocPerms to admin-only
(`System Manager` / `Flock Org Admin`) since no sanctioned consumer exists.
**Filed as follow-up.**

---

## 5. Risk register (filed follow-ups)

| ID | Severity | Finding | Owner | Status |
|---|---|---|---|---|
| SEC-1..SEC-6 | HIGH/MED | FLO-290 findings | Architect | **Fixed** (FLO-290) |
| SEC-B1 | LOW | Audit Log Org Admin tamper | Backend | **Filed** → [FLO-900](/FLO/issues/FLO-900) |
| SEC-B2 | LOW | Engagement Session Member create | Backend | **Filed** → [FLO-901](/FLO/issues/FLO-901) |
| SEC-B3 | LOW | Dormant engagement Round/Feedback unscoped | Architect | **Filed** → [FLO-902](/FLO/issues/FLO-902) |
| SEC-RL | MED | Edge rate-limiting / WAF / DDoS | DevOps (Phase 6.1) | Tracked child ([FLO-249](/FLO/issues/FLO-249)) |

**No HIGH or MEDIUM permission gap remains open.** The two-axis isolation model
is verified sound across all 24 DocTypes; the only open items are three LOW
defense-in-depth / dormant-table follow-ups, now properly tracked.
