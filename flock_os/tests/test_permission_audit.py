"""
Phase 6.2 security & permission-audit regression tests ([FLO-290](/FLO/issues/FLO-290)).

These pin the **role-level (docperm)** layer of the two-axis authorization model
(ADR-0001 §6). They complement the runtime-scoping suites
(:mod:`flock_os.tests.test_permissions`, :mod:`test_tenant_isolation`,
:mod:`test_permission_matrix`) which exhaustively cover the *group-axis*
``permission_query_conditions`` hook + the ``assert_*_scope`` guards. That
coverage proved the row-level isolation *mechanism*; this suite proves the
**Frappe DocPerm role→capability map** is least-privilege — the layer the
runtime hook sits on top of, and the layer the FLO-290 audit hardened.

Runs under plain ``pytest`` (no bench): it parses each DocType JSON and asserts
the locked least-privilege contract. The transport-layer caller-authorization
guards added by FLO-290 (announcement author branch scope, explicit-member
registration authority) ride the same ``permissions.assert_branch_scope`` guard
whose deny-path is already pinned in :mod:`test_tenant_isolation`; the docperm
rules below are the JSON-layer backstop that makes those guards meaningful
(a role with no ``write`` cannot bypass the guard by editing the row directly).

Coverage:
* Only the known Flock role catalog (+ System Manager) holds any permission.
* ``set_user_permissions`` is never granted to a non-System role.
* No DocType carries a duplicate ``(role, permlevel)`` permission row.
* HIGH-1: ``Flock Branch Admin`` is read-only on ``Flock Branch Admin Scope``
  (the User-Permission materialization table) — no privilege escalation.
* HIGH-2: ``Flock Event Approval`` has no duplicate permlevel-0 row and grants
  permlevel 1 (proposed scope) + permlevel 2 (decision fields).
* MEDIUM-5: ``Flock Announcement`` lets ``Flock Group Leader`` write the
  permlevel-1 scope-targeting fields (so authoring is functional) while the
  backend re-asserts the author's branch scope.
* Member/Visitor do not hold inappropriate write/create on sensitive doctypes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

DOCTYPE_DIR = Path(__file__).resolve().parent.parent / "flock_os" / "doctype"

#: The sensitive DocTypes the FLO-290 audit scoped (§2): every transactional
#: / PII-bearing surface plus the two permission-infra tables.
SENSITIVE_DOCTYPES: tuple[str, ...] = (
	"Flock Member",
	"Flock Group Member",
	"Flock Event Registration",
	"Flock Attendance Record",
	"Flock Engagement Session",
	"Flock Engagement Round",
	"Flock Engagement Feedback",
	"Flock Gathering",
	"Flock Announcement",
	"Flock Event Approval",
	"Flock Event Invitation",
	"Flock Branch Admin Scope",
	"Flock Audit Log",
)

#: The complete allowed role catalog (ADR §6.4 — mirrors fixtures.FLOCK_ROLES).
#: No DocType may grant a permission to a role outside this set ∪ System Manager.
ALLOWED_ROLES: frozenset[str] = frozenset(
	{
		"Flock Org Admin",
		"Flock Branch Admin",
		"Flock Group Leader",
		"Flock Member",
		"Flock Visitor",
		"Flock Auditor",
		"System Manager",
	}
)


def _slug(name: str) -> str:
	return name.lower().replace(" ", "_")


def _load(name: str) -> dict:
	path = DOCTYPE_DIR / _slug(name) / f"{_slug(name)}.json"
	assert path.exists(), f"Missing DocType JSON for {name}: {path}"
	with path.open() as f:
		return json.load(f)


def _perms(name: str) -> list[dict]:
	"""The ``permissions`` array for ``name`` (role → capability rows)."""
	return _load(name).get("permissions", [])


def _perm_rows(name: str, *, role: str, permlevel: int = 0) -> list[dict]:
	return [p for p in _perms(name) if p.get("role") == role and p.get("permlevel", 0) == permlevel]


def _has(name: str, role: str, capability: str, *, permlevel: int = 0) -> bool:
	"""True iff ``role`` holds ``capability`` (==1) at ``permlevel`` on ``name``."""
	return any(p.get(capability) == 1 for p in _perm_rows(name, role=role, permlevel=permlevel))


# --------------------------------------------------------------------------- #
# Catalog hygiene — the global least-privilege invariants (every sensitive DT).
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", SENSITIVE_DOCTYPES)
def test_only_known_roles_hold_permissions(name):
	# No unexpected role (e.g. "Desk User", "Guest", a stale custom role) holds
	# any capability on a sensitive DocType — the audit found none, and this
	# pins it so a future fixture drift cannot silently widen access.
	unknown = {p.get("role") for p in _perms(name)} - ALLOWED_ROLES
	assert not unknown, f"{name} grants permissions to unknown roles: {unknown}"


@pytest.mark.parametrize("name", SENSITIVE_DOCTYPES)
def test_set_user_permissions_never_granted_to_non_system_roles(name):
	# ``set_user_permissions`` lets a role rewrite another user's User
	# Permissions — a privilege-escalation primitive. Only System Manager may
	# ever hold it; Flock roles never. (Audit found none granted; this pins it.)
	for p in _perms(name):
		if p.get("set_user_permissions") == 1:
			assert p.get("role") == "System Manager", (
				f"{name}: role {p.get('role')!r} holds set_user_permissions (escalation)."
			)


@pytest.mark.parametrize("name", SENSITIVE_DOCTYPES)
def test_no_duplicate_role_permlevel_permission_rows(name):
	# A duplicate (role, permlevel) row is always a bug — either a redundant
	# subset (H2 on Flock Event Approval) or a mis-pasted permlevel grant. Pin
	# uniqueness so the regression the audit fixed cannot recur.
	seen: set[tuple[str, int]] = set()
	for p in _perms(name):
		key = (p.get("role"), p.get("permlevel", 0))
		assert key not in seen, f"{name}: duplicate permission row for {key}"
		seen.add(key)


# --------------------------------------------------------------------------- #
# HIGH-1 — Flock Branch Admin Scope privilege escalation (docperm hardening).
#
# The Branch Admin Scope table materializes a user's branch subtree into User
# Permission rows on validate. Granting Flock Branch Admin write/create/delete
# here lets a scoped admin widen their own (or anyone's) root → see every
# branch. Fixed in FLO-290: Branch Admin is read-only; only Org Admin +
# System Manager manage scopes.
# --------------------------------------------------------------------------- #


def test_branch_admin_is_read_only_on_branch_admin_scope():
	name = "Flock Branch Admin Scope"
	for cap in ("create", "write", "delete", "share", "set_user_permissions"):
		assert not _has(name, "Flock Branch Admin", cap), (
			f"{name}: Flock Branch Admin must not hold {cap} (privilege escalation)."
		)
	# Read/report remain (a branch admin may view who administers their subtree).
	assert _has(name, "Flock Branch Admin", "read")
	assert _has(name, "Flock Branch Admin", "report")


def test_branch_admin_scope_write_restricted_to_org_admin_and_system_manager():
	name = "Flock Branch Admin Scope"
	writers = {p.get("role") for p in _perms(name) if p.get("permlevel", 0) == 0 and p.get("write") == 1}
	# Only the two trusted global roles may mutate the scope table.
	assert writers <= {"System Manager", "Flock Org Admin"}
	assert "Flock Branch Admin" not in writers


def test_branch_admin_cannot_widen_own_scope_via_create_or_delete():
	name = "Flock Branch Admin Scope"
	# A branch admin must not be able to create a broader scope row or delete a
	# peer's (denial-of-access / scope-tampering). Both are escalation surfaces.
	assert not _has(name, "Flock Branch Admin", "create")
	assert not _has(name, "Flock Branch Admin", "delete")


# --------------------------------------------------------------------------- #
# HIGH-2 — Flock Event Approval duplicate permlevel-0 row + missing permlevels.
#
# Audit found a duplicate "Flock Org Admin" permlevel-0 row (a mis-paste) and
# NO permlevel 1 (proposed_registration_scope) / permlevel 2 (decision fields)
# grants — so the scope proposal + decision fields were unwritable via the
# standard form. Fixed in FLO-290.
# --------------------------------------------------------------------------- #


def test_event_approval_has_single_org_admin_permlevel_zero_row():
	rows = _perm_rows("Flock Event Approval", role="Flock Org Admin", permlevel=0)
	assert len(rows) == 1, "Flock Event Approval must have exactly one Org Admin pl0 row"


def test_event_approval_proposed_scope_permlevel_is_writable():
	# permlevel 1 = proposed_registration_scope. The requesting leader proposes;
	# admins may override. All four managing roles get R/W at permlevel 1.
	name = "Flock Event Approval"
	for role in ("System Manager", "Flock Org Admin", "Flock Branch Admin", "Flock Group Leader"):
		assert _has(name, role, "read", permlevel=1), f"{name}: {role} missing pl1 read"
		assert _has(name, role, "write", permlevel=1), f"{name}: {role} missing pl1 write"


def test_event_approval_decision_permlevel_grants_are_present():
	# permlevel 2 = final_decision_by / final_decision_at / rejection_reason.
	# Decided by the resolved approver chain (admins write; leader + auditor read).
	name = "Flock Event Approval"
	for role in ("System Manager", "Flock Org Admin", "Flock Branch Admin"):
		assert _has(name, role, "write", permlevel=2), f"{name}: {role} missing pl2 write"
	for role in ("Flock Group Leader", "Flock Auditor"):
		assert _has(name, role, "read", permlevel=2), f"{name}: {role} missing pl2 read"


# --------------------------------------------------------------------------- #
# MEDIUM-5 — Flock Announcement Group Leader permlevel-1 grant.
#
# Group Leader had permlevel-0 create/write but no permlevel-1 grant, so the
# scope-targeting fields (branch/group/audience_role/priority/channels) were
# unwritable when authoring — a functional gap. Fixed in FLO-290 alongside the
# backend ``_assert_author_branch_scope`` guard (source of truth for cross-branch).
# --------------------------------------------------------------------------- #


def test_announcement_group_leader_can_write_scope_targeting_fields():
	name = "Flock Announcement"
	assert _has(name, "Flock Group Leader", "read", permlevel=1)
	assert _has(name, "Flock Group Leader", "write", permlevel=1)


def test_announcement_member_remains_read_only():
	# The compose/audience fields are admin/leader-only; members only read the
	# announcement body (permlevel 0 read). No escalation via the permlevel fix.
	name = "Flock Announcement"
	assert _has(name, "Flock Member", "read")
	for cap in ("write", "create", "delete"):
		assert not _has(name, "Flock Member", cap)


# --------------------------------------------------------------------------- #
# Member/Visitor least-privilege — no inappropriate write on sensitive data.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
	"name", ["Flock Member", "Flock Attendance Record", "Flock Announcement", "Flock Gathering"]
)
def test_flock_member_has_no_write_on_member_scoped_doctypes(name):
	# A plain member must not mutate member rosters, attendance, announcements,
	# or gatherings. (Self-registration / feedback create paths are scoped
	# elsewhere.) This is the deny-default the audit confirmed.
	for cap in ("write", "delete"):
		assert not _has(name, "Flock Member", cap), (
			f"{name}: Flock Member must not hold {cap} (least privilege)."
		)


def test_visitor_write_is_confined_to_self_registration():
	# Flock Visitor may only self-register (Flock Event Registration). It must
	# not hold write/create on any other sensitive DocType.
	for name in SENSITIVE_DOCTYPES:
		if name == "Flock Event Registration":
			continue  # self-registration is the sanctioned visitor write surface
		for cap in ("write", "create", "delete"):
			assert not _has(name, "Flock Visitor", cap), (
				f"{name}: Flock Visitor must not hold {cap} (visitor = self-register only)."
			)
