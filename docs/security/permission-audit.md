# Flock OS — Security & Permission Audit (Phase 6.2)

> Scope: role-level (DocPerm) + row-level (subtree) isolation across the
> org/branch/group tree, and the public/portal REST surface. VM-independent —
> audited against the shipped MVP on the local bench, no cloud, no spend.
>
> Parent: [FLO-231](/FLO/issues/FLO-231) (Phase 6) · Audit issue:
> [FLO-290](/FLO/issues/FLO-290) · ADR-0001 §6 (two-axis authorization model).

This document is the **severity-rated findings list** for acceptance criterion
#1 of [FLO-290](/FLO/issues/FLO-290). It covers the four audit domains:
(1) role-level DocPerm, (2) row-level subtree isolation, (3) public/portal
surface, (4) rate-limit / abuse posture. Every HIGH finding is either **fixed
in code** in this slice or filed as a tracked child issue.

## Verification

```
ruff check . && ruff format --check . && pytest
```

- `pytest`: **1115 passed**, including the new `flock_os/tests/test_permission_audit.py`
  (52 docperm least-privilege assertions). Coverage did not drop — the new file
  adds net-new assertions at the DocPerm layer (previously uncovered; the
  existing `test_permissions` / `test_tenant_isolation` / `test_permission_matrix`
  cover the runtime scoping hook, not the role→capability map).
- `bench run-tests --app flock_os`: runs once a headless Frappe image is wired
  (README §"Running the tests"); the DocType-controller transport paths
  (`register_for_event`, `publish_announcement`, …) are covered there. The pure
  halves + the docperm JSON contract are pinned under plain pytest.

## TL;DR

The **two-axis authorization model is sound** (ADR-0001 §6): the group-axis
`permission_query_conditions` hook + the branch-axis User-Permission
materialization compose correctly, and the existing
`test_tenant_isolation` / `test_permission_matrix` suites already prove a
scoped leader **cannot** read/write a foreign subtree or cross-branch row on any
`SCOPED_DOCTYPES` entry. No cross-tenant data leakage was found in the runtime
scoping machinery.

The audit found **five code-level issues**, all in the layers *on top of* the
scoping hook: one privilege-escalation primitive (HIGH), one functional +
least-privilege gap (HIGH), and three defense-in-depth / least-privilege gaps
(MEDIUM). All five are **fixed in this slice**. Three additional hardening items
are documented as tracked child issues (rate-limiting is Phase-6.1-edge-gated by
scope).

---

## 1. Role-level (DocPerm) audit

Every `flock_*` DocType's `permissions[]` array was reviewed for least
privilege. Findings:

### [HIGH] SEC-1 — `Flock Branch Admin Scope` privilege escalation (FIXED)

**Where:** `flock_os/flock_os/doctype/flock_branch_admin_scope/` (DocPerm).
**Finding:** `Flock Branch Admin` held `create`/`write`/`delete` on the very
table that materializes a user's branch subtree into Frappe **User Permission**
rows (`FlockBranchAdminScope.validate` → `FrappeUserPermissionSyncer.sync_branch_scope`,
which writes UP rows with `ignore_permissions=True`). A scoped Branch Admin could
create a scope row whose `branch` is the org-root for their own `user`, and on
validate their User-Permission subtree would be re-materialized to the **entire
org** — a self-serve privilege escalation past their assigned subtree. They
could likewise delete peer admins' scope rows (access revocation / DoS).
**Severity basis:** a single low-privilege actor can widen their own read scope
org-wide and across the branch axis that everything else relies on.
**Fix:** DocPerm changed to read/report-only for `Flock Branch Admin`; only
`System Manager` + `Flock Org Admin` manage scope rows. Pinned by
`test_permission_audit.py::test_branch_admin_is_read_only_on_branch_admin_scope`
(and the renamed `test_doctype_schema.py::test_flock_branch_admin_scope_branch_admin_is_read_only`).

### [HIGH] SEC-2 — `Flock Event Approval` duplicate perm row + missing permlevels (FIXED)

**Where:** `flock_os/flock_os/doctype/flock_event_approval/` (DocPerm).
**Finding:** (a) a **duplicate** `Flock Org Admin` row at permlevel 0 — the
second was a mis-pasted subset (`read/report/submit/write`, no create/delete);
(b) **no** permlevel 1 (`proposed_registration_scope`) or permlevel 2
(`final_decision_by`/`final_decision_at`/`rejection_reason`) grants existed, so
those fields were **unwritable through the standard form** — a leader could not
propose a registration scope and admins could not record the decision via DocPerm.
The duplicate row is a latent least-privilege hazard (a copy-paste away from
granting an unintended capability).
**Fix:** duplicate removed; permlevel 1 R/W granted to System Manager / Org
Admin / Branch Admin / Group Leader (the requester proposes); permlevel 2 R/W to
System Manager / Org Admin / Branch Admin, R to Group Leader + Auditor (decision
fields). Pinned by `test_event_approval_*` in `test_permission_audit.py` and the
generic `test_no_duplicate_role_permlevel_permission_rows`.

### [MEDIUM] SEC-3 — `Flock Announcement` Group Leader could not author scope-targeting fields (FIXED)

**Where:** `flock_os/flock_os/doctype/flock_announcement/` (DocPerm).
**Finding:** `Flock Group Leader` had permlevel-0 `create`/`write` (`if_owner`)
but **no permlevel-1 grant**, so the scope fields (`branch`, `group`,
`audience_role`, `priority`, `channels`) were unwritable when authoring — the
leader could start an announcement but not target it. Functional gap with a
security tint (the next finding addresses the backend re-check).
**Fix:** permlevel 1 R/W granted to `Flock Group Leader` (read stays for Member).
Pinned by `test_announcement_group_leader_can_write_scope_targeting_fields`.

### Catalog hygiene (no findings — confirmed clean)

- **Roles:** every permission row across all sensitive DocTypes uses a role in
  the Flock catalog (`Flock Org Admin` / `Flock Branch Admin` / `Flock Group
  Leader` / `Flock Member` / `Flock Visitor` / `Flock Auditor`) or `System
  Manager`. No `Desk User`, `Guest`, or stale custom role. Pinned by
  `test_only_known_roles_hold_permissions`.
- **`set_user_permissions`:** granted to **no** role anywhere (only `Flock
  Organization` explicitly sets it `0` on System Manager). Pinned by
  `test_set_user_permissions_never_granted_to_non_system_roles`.
- **`if_owner`:** used appropriately on 6 DocTypes (Member self-read, Group
  Leader owner-scoping on Group Member / Group / Gathering / Announcement /
  Event Approval).
- **Member/Visitor least privilege:** confirmed — Member is read-only on
  Member/Attendance/Announcement/Gathering; Visitor write is confined to
  self-registration (`Flock Event Registration`) and nothing else. Pinned by
  `test_flock_member_has_no_write_on_member_scoped_doctypes` and
  `test_visitor_write_is_confined_to_self_registration`.

### Documented for follow-up (filed as children)

- **[LOW] SEC-A1** — `Flock Audit Log`: `Flock Org Admin` holds full
  `write`/`delete` on the audit trail (tamper capability for a trusted role).
  Recommend append-only for all but System Manager. Filed as a child.
- **[LOW] SEC-A2** — `Flock Engagement Session`: `Flock Member` holds `create`
  (no write). Sessions are facilitator-created; a member spawning session rows
  is noise rather than a leak. Recommend drop `create` for Member. Filed as a
  child.

---

## 2. Row-level / subtree isolation audit

**Verdict: sound.** No new findings — the existing suites already exhaustively
prove isolation; this audit re-confirmed them and added docperm backstops.

The core check (FLO-290 §2): a scoped leader must only see/act on their
subtree, and be **denied** cross-scope access to `flock_member`,
`flock_group_member`, `flock_event_registration`, `flock_attendance_record`,
`flock_engagement_*`, `flock_gathering`, `flock_announcement`. Confirmed via:

- `flock_os/permissions.py` — the single row-level chokepoint
  (`permission_query_conditions` hook → `resolve_leader_scope` →
  `build_group_scope_sql` → `assert_branch_scope` / `assert_group_scope`).
- `test_tenant_isolation.py` — cross-branch deny at the guard (symmetric),
  parent-excluded/descendant-included, admin subtrees disjoint, null-branch
  org-wide visibility.
- `test_permission_matrix.py` — the full role × DocType × row-position matrix;
  guard/hook parity cell-by-cell; deny-default for forged/escalated group and
  cross-branch access; approval-authority (`assert_approval_scope`) confinement.

The `SCOPED_DOCTYPES` list + `MEMBER_ANCHORED_DOCTYPES` map correctly cover
every sensitive transactional DocType (the hook narrows them uniformly). The
branch axis (native User Permissions, materialized by
`FlockBranchAdminScope` + `FrappeUserPermissionSyncer`) and the group axis
(custom nested-set hook) compose independently — same-branch is not sufficient
(group-axis subtree containment is enforced separately), per
`test_cross_subtree_foreign_denied_at_group_guard_but_passes_branch_guard`.

### [MEDIUM] SEC-4 — announcement publish/preview/schedule did not re-assert the caller's branch scope (FIXED)

**Where:** `flock_os/scheduling.py` (`preview_audience`, `publish_announcement`,
`schedule_announcement`).
**Finding:** `validate_announcement_scope` enforces the announcement's internal
group-branch-binding and branch-in-org, but **none** of the `@frappe.whitelist()`
surfaces asserted that the *session user* has branch scope over the target
`branch`. The compose UI guards this client-side
(`portal.assert_target_in_context`), but "never trust the client" — a forged
request could attempt cross-subtree publish (bounded by DocPerm read on the
announcement row, but not by the author's own subtree).
**Fix:** added `_assert_author_branch_scope(branch)` (backed by
`permissions.assert_branch_scope`, installable seam for tests) called in all
three surfaces before audience resolution/fan-out. Org Admin/Auditor pass; a
Branch Admin/Group Leader passes only for branches in their materialized UP
subtree. Deny-path pinned by `test_tenant_isolation`; the pure scheduling logic
is decoupled via `install_author_scope_guard` in `test_scheduling.py`.

---

## 3. Public / portal surface audit

Reviewed every `@frappe.whitelist()` / `@_whitelist()` method in `portal.py`,
`realtime_views.py`, `engagement_api.py`, `registrations.py` (pure half) + the
`flock_event_registration` transport controller, `scheduling.py`,
`engagement_api.py`.

### [MEDIUM] SEC-5 — `register_for_event` did not authorize an explicitly-passed member (FIXED)

**Where:** `flock_os/flock_os/doctype/flock_event_registration/flock_event_registration.py`.
**Finding:** `register_for_event(gathering, member=None, …)` resolves the member
from the session user when omitted (self-registration), but when `member` was
passed explicitly there was **no check that the caller is authorized** to
register that member. Any authenticated user could register arbitrary
in-scope members (seat-claim / spam). The eligibility gate confines the
*registrant's* scope, not the *caller's* authority.
**Fix:** when `member` differs from the session user's linked member, the
endpoint now asserts the caller's branch scope over the gathering's branch
(`permissions.assert_branch_scope`) — i.e. only a leader/admin in the event's
branch may register on someone's behalf. Self-registration is unchanged.

### [MEDIUM] SEC-6 — `portal.targetable_branches` read the wrong User-Permission columns (FIXED)

**Where:** `flock_os/portal.py` (`FrappeComposeGateway.targetable_branches`).
**Finding:** the scoped-role branch read filtered `{"doctype": "Flock Branch"}`
and plucked `doc.name` — but Frappe User Permission's link/value columns are
`allow` / `for_value` (as used correctly by
`FrappePermissionGateway.list_branch_user_permissions`). The wrong column names
returned an **empty** targetable set for every Branch Admin / Group Leader, so
the compose picker offered no branches and `assert_target_in_context` rejected
every target (fail-closed — a feature DoS, not a leak).
**Fix:** corrected to `{"allow": "Flock Branch"}` / `pluck="for_value"`,
matching the permission spine (DRY — one column vocabulary).

### Confirmed clean

- **`realtime_views.can_join_event_room`** — delegates to the pure
  `realtime.event_room_join_allowed` branch-scope gate; no IDOR, no unscoped
  room join. (`test_realtime_room_scope.py`.)
- **`engagement_api`** — `create_session` asserts branch scope; facilitator-only
  lifecycle (`_assert_facilitator`); player paths (`join`/`participate`/`bulk`)
  authenticate via **signed session tickets** (signature binds
  session+attendee+window) — the engagement runtime is a live physical-event
  surface where the ticket is the auth, not membership scope. `out_of_scope` is
  a flag for facilitator review, never a silent admit. Per-device Redis throttle
  (§6.6) bounds participation fan-out. Clean by design.
- **registration transport** — `cancel_registration` (`_assert_may_act_on_registration`:
  self/leader/admin), `check_in_registration` (`_assert_may_check_in`: leader+
  admin role + branch scope), `register_bulk` / `get_registration_dashboard`
  (branch-scope guard) all enforce scope correctly.
- **`portal.build_compose_context`** — offered branch/group sets come verbatim
  from the gateway; `assert_target_in_context` rejects any forged target — the
  no-leakage boundary (now backed by the corrected `targetable_branches`).

---

## 4. Rate-limit / abuse posture (documented; edge work is Phase-6.1-gated)

Current application-level rate limiting:

| Endpoint class | App-level limit | Mechanism |
| --- | --- | --- |
| `engagement.participate` / `bulk_attendance` | **Yes** — ≤5/s/device | Redis sliding-window throttle (`engagement_frappe.throttle_allows`, §6.6) |
| Realtime Socket.IO `connect` / room `join` | **No app-level** | Relies on Frappe session auth + `can_join_event_room` scope gate |
| `registrations.register_for_event` | **No app-level** | Eligibility/window/capacity gates only |
| `engagement.join_session` (ticket issue) | **No app-level** | Signed-ticket auth only |

**Recommendation (not implemented here — out of scope, [FLO-249](/FLO/issues/FLO-249) /
Phase 6.1):** the high-fan-out public endpoints (realtime connect, registration
create, engagement join) need minimal prod hardening at the **edge** (Cloudflare
rate-limiting / WAF / LB), not in-app, to survive a 15k-attendee burst and blunt
abuse. Captured as a tracked child so it is not lost. TLS, edge DDoS, and
managed-DB hardening similarly land with Phase 6.1 provisioning.

---

## Risk register (filed children)

| ID | Severity | Owner | Status |
| --- | --- | --- | --- |
| SEC-1 Branch Admin Scope escalation | HIGH | Architect | **Fixed** (this slice) |
| SEC-2 Event Approval permlevels | HIGH | Architect | **Fixed** (this slice) |
| SEC-3 Announcement Leader pl1 | MEDIUM | Architect | **Fixed** (this slice) |
| SEC-4 Announcement author scope | MEDIUM | Architect | **Fixed** (this slice) |
| SEC-5 register_for_event authority | MEDIUM | Architect | **Fixed** (this slice) |
| SEC-6 portal targetable_branches | MEDIUM | Architect | **Fixed** (this slice) |
| SEC-A1 Audit Log tamper | LOW | Backend | Filed child |
| SEC-A2 Engagement Session member create | LOW | Backend | Filed child |
| SEC-RL Edge rate-limiting | MEDIUM | DevOps (Phase 6.1) | Filed child |
