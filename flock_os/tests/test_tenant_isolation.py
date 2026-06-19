"""
Phase-1 tenant-isolation scenario suite (FLO-21).

These are the **end-to-end permission-matrix scenarios** the spec ([FLO-5](/FLO/issues/FLO-5)
§4.5 / §4.7) and ADR-0001 §6 make QA-owned guarantees about. They complement the
fine-grained unit tests in :mod:`flock_os.tests.test_permissions` by composing
the same primitives into the real-world isolation matrix a multi-branch org
relies on, so the **matrix is asserted explicitly** rather than implied by
scattered unit cases.

Scope pinned here (spec §4.7 guarantees):

* **§4.7 #1 — cross-branch leakage is impossible via standard reads.** A branch
  actor cannot escape their subtree; two branches never see each other's data.
* **Org Admin / Auditor see the full tree** (no branch scoping).
* **§4.3 rec #6 — the FLO-21-owned "broader scope wins" regression.** A dual-role
  user (Group Leader **+** Branch Admin) is treated as Branch Admin: they get the
  broader branch scope and **no** group-axis fragment. A plain Group Leader gets
  the narrower group fragment. This is the exact test the spec delegates to FLO-21.
* **Null-branch org-wide rows** are visible to any authenticated user.

The pure halves (subtree materialization, the `permission_query_conditions`
fragment, and the `assert_*_scope` guards) are exercised against an in-memory
gateway over a fully-known two-branch world — no Frappe site required (same
hexagonal-port discipline as the rest of the project-level gate).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from flock_os import permissions as perms
from flock_os.permissions import (
	BYPASS_ROLES,
	GroupBounds,
	PermissionGateway,
)

# --------------------------------------------------------------------------- #
# A fully-known two-branch world.
# --------------------------------------------------------------------------- #
# Org tree (two tenant subtrees under a root HQ):
#
#   HQ                       (Org Admin / Auditor see all; no branch UP)
#    ├── North               (Branch Admin root — sees North + North-A + North-A1)
#    │    ├── North-A
#    │    │    └── North-A1
#    │    └── North-B
#    └── South               (independent branch — hard isolation target)
#         └── South-A
#
# Group tree (within North; leader M-lead leads a subtree):
#
#   G-North (lft 1, rgt 6, leader M-lead)   ← Branch Admin also holds Leader here
#    ├── G-Alpha (lft 2, rgt 3)
#    └── G-Beta  (lft 4, rgt 5)
#
# A dual-role user (Branch Admin + Group Leader) is the §4.3 rec #6 subject.

BRANCH_PARENT_OF: dict[str, str | None] = {
	"HQ": None,
	"North": "HQ",
	"North-A": "North",
	"North-A1": "North-A",
	"North-B": "North",
	"South": "HQ",
	"South-A": "South",
}
BRANCH_CHILDREN_OF: dict[str, list[str]] = {
	"HQ": ["North", "South"],
	"North": ["North-A", "North-B"],
	"North-A": ["North-A1"],
	"North-A1": [],
	"North-B": [],
	"South": ["South-A"],
	"South-A": [],
}

NORTH_SUBTREE = perms.compute_branch_subtree(
	"North", parent_of=BRANCH_PARENT_OF, children_of=BRANCH_CHILDREN_OF
)
SOUTH_SUBTREE = perms.compute_branch_subtree(
	"South", parent_of=BRANCH_PARENT_OF, children_of=BRANCH_CHILDREN_OF
)


def _gb(name: str, lft: int, rgt: int) -> GroupBounds:
	return GroupBounds(name=name, lft=lft, rgt=rgt)


G_NORTH_BOUNDS = _gb("G-North", 1, 6)


@dataclass
class ScenarioGateway(PermissionGateway):
	"""In-memory gateway over the two-branch world for isolation scenarios."""

	roles_by_user: dict[str, frozenset[str]] = field(default_factory=dict)
	member_by_user: dict[str, str] = field(default_factory=dict)
	led_bounds_by_member: dict[str, tuple[GroupBounds, ...]] = field(default_factory=dict)
	joined_by_member: dict[str, tuple[str, ...]] = field(default_factory=dict)
	branch_ups_by_user: dict[str, tuple[str, ...]] = field(default_factory=dict)
	group_bounds_by_name: dict[str, GroupBounds] = field(default_factory=dict)

	def get_user_roles(self, user: str) -> frozenset[str]:
		return self.roles_by_user.get(user, frozenset())

	def resolve_member_for_user(self, user: str) -> str | None:
		return self.member_by_user.get(user)

	def fetch_led_group_bounds(self, member: str) -> tuple[GroupBounds, ...]:
		return self.led_bounds_by_member.get(member, ())

	def fetch_joined_group_names(self, member: str) -> tuple[str, ...]:
		return self.joined_by_member.get(member, ())

	def fetch_group_bounds(self, name: str) -> GroupBounds | None:
		return self.group_bounds_by_name.get(name)

	def list_branch_user_permissions(self, user: str) -> tuple[str, ...]:
		return self.branch_ups_by_user.get(user, ())


def _passthrough(value: object) -> str:
	return f"'{value}'"


def _north_branch_admin_gateway() -> ScenarioGateway:
	return ScenarioGateway(
		roles_by_user={"ba@north": frozenset({perms.ROLE_BRANCH_ADMIN})},
		branch_ups_by_user={"ba@north": NORTH_SUBTREE},
	)


def _dual_role_gateway() -> ScenarioGateway:
	# A Group Leader who is ALSO a Branch Admin (§4.3 rec #6 subject). They lead
	# G-North AND administer the North subtree.
	return ScenarioGateway(
		roles_by_user={"dual@north": frozenset({perms.ROLE_GROUP_LEADER, perms.ROLE_BRANCH_ADMIN})},
		member_by_user={"dual@north": "M-lead"},
		led_bounds_by_member={"M-lead": (G_NORTH_BOUNDS,)},
		branch_ups_by_user={"dual@north": NORTH_SUBTREE},
		group_bounds_by_name={"G-North": G_NORTH_BOUNDS},
	)


def _plain_leader_gateway() -> ScenarioGateway:
	return ScenarioGateway(
		roles_by_user={"lead@north": frozenset({perms.ROLE_GROUP_LEADER})},
		member_by_user={"lead@north": "M-lead"},
		led_bounds_by_member={"M-lead": (G_NORTH_BOUNDS,)},
		joined_by_member={"M-lead": ()},
		group_bounds_by_name={"G-North": G_NORTH_BOUNDS},
	)


# --------------------------------------------------------------------------- #
# §4.7 #1 — cross-branch leakage is impossible via standard reads.
# --------------------------------------------------------------------------- #


class TestCrossBranchIsolation:
	"""The core tenant-isolation guarantee: branch actors cannot escape."""

	def test_branch_admin_cannot_access_sibling_branch_at_guard(self):
		# A North admin touching a South row → FlockPermissionError (the guard is
		# the single chokepoint every @frappe.whitelist() endpoint asserts).
		gw = _north_branch_admin_gateway()
		with pytest.raises(perms.FlockPermissionError):
			perms.assert_branch_scope(doc_branch="South", user="ba@north", gateway=gw)
		with pytest.raises(perms.FlockPermissionError):
			perms.assert_branch_scope(doc_branch="South-A", user="ba@north", gateway=gw)

	def test_branch_admin_can_access_own_subtree_at_guard(self):
		gw = _north_branch_admin_gateway()
		# Every node in the North subtree is reachable.
		for branch in NORTH_SUBTREE:
			perms.assert_branch_scope(doc_branch=branch, user="ba@north", gateway=gw)

	def test_isolation_is_symmetric_both_directions(self):
		# A South admin is equally barred from North — isolation is mutual.
		gw = ScenarioGateway(
			roles_by_user={"ba@south": frozenset({perms.ROLE_BRANCH_ADMIN})},
			branch_ups_by_user={"ba@south": SOUTH_SUBTREE},
		)
		with pytest.raises(perms.FlockPermissionError):
			perms.assert_branch_scope(doc_branch="North", user="ba@south", gateway=gw)
		with pytest.raises(perms.FlockPermissionError):
			perms.assert_branch_scope(doc_branch="North-A1", user="ba@south", gateway=gw)

	def test_admin_subtrees_are_disjoint(self):
		# The materialized subtrees share no node — no branch can appear in two
		# admins' scopes, so no read path can cross tenant lines.
		assert set(NORTH_SUBTREE).isdisjoint(SOUTH_SUBTREE)

	def test_branch_admin_descendants_included_but_parent_excluded(self):
		# A North admin sees North-A1 (descendant) but NOT HQ (ancestor) — scope
		# grows down the tree, never up.
		gw = _north_branch_admin_gateway()
		perms.assert_branch_scope(doc_branch="North-A1", user="ba@north", gateway=gw)
		with pytest.raises(perms.FlockPermissionError):
			perms.assert_branch_scope(doc_branch="HQ", user="ba@north", gateway=gw)

	def test_can_access_branch_predicate_matches_guard(self):
		# The list-query predicate and the guard agree: North-A in, South out.
		gw = _north_branch_admin_gateway()
		allowed = gw.list_branch_user_permissions("ba@north")
		roles = list(gw.get_user_roles("ba@north"))
		assert perms.can_access_branch(branch="North-A", allowed_branches=allowed, roles=roles) is True
		assert perms.can_access_branch(branch="South", allowed_branches=allowed, roles=roles) is False


# --------------------------------------------------------------------------- #
# Org Admin / Auditor — the full-tree viewers (§4.1 / §4.2).
# --------------------------------------------------------------------------- #


class TestFullTreeViewers:
	"""Org Admin + Auditor bypass branch scoping and see every branch."""

	@pytest.mark.parametrize("role", [perms.ROLE_ORG_ADMIN, perms.ROLE_AUDITOR])
	def test_global_roles_bypass_branch_scope(self, role):
		# No branch User Permission → every branch (including the sibling tenant)
		# is reachable at the guard. This is the "admin sees full tree" guarantee.
		gw = ScenarioGateway(roles_by_user={"global@flock": frozenset({role})})
		for branch in ("HQ", "North", "North-A1", "South", "South-A"):
			perms.assert_branch_scope(doc_branch=branch, user="global@flock", gateway=gw)

	@pytest.mark.parametrize("role", [perms.ROLE_ORG_ADMIN, perms.ROLE_AUDITOR])
	def test_global_roles_are_bypass_users(self, role):
		assert perms.is_bypass_user([role]) is True

	def test_auditor_is_in_bypass_catalog(self):
		# §4.1: Auditor is read-only across ALL branches (no branch UP restriction).
		assert perms.ROLE_AUDITOR in BYPASS_ROLES

	@pytest.mark.parametrize("role", [perms.ROLE_ORG_ADMIN, perms.ROLE_AUDITOR])
	def test_global_roles_get_no_group_scope_fragment(self, role):
		# Bypass roles emit no group-axis fragment — Frappe's DocPerm layer applies
		# and they see the full group tree too.
		perms.install_gateway(
			ScenarioGateway(
				roles_by_user={"global@flock": frozenset({role})},
				member_by_user={"global@flock": "M-any"},
				led_bounds_by_member={"M-any": (G_NORTH_BOUNDS,)},
			)
		)
		try:
			assert perms.get_group_scoped_conditions(doctype="Flock Group", user="global@flock") == ""
			assert perms.has_group_scope("Flock Group", "global@flock") is False
		finally:
			perms.install_gateway(perms.NullPermissionGateway())


# --------------------------------------------------------------------------- #
# §4.3 rec #6 — the FLO-21-owned "broader scope wins" dual-role regression.
# --------------------------------------------------------------------------- #


class TestBroaderScopeWins:
	"""A dual-role Leader+BranchAdmin is treated as Branch Admin (broader wins).

	Spec §4.3 rec #6 explicitly delegates this regression to FLO-21: a dual-role
	user is asserted to see the broader branch scope (no group fragment), while a
	plain Leader sees only their narrower led subtree (group fragment present).
	"""

	def test_dual_role_emits_no_group_fragment(self):
		# The dual-role user leads G-North BUT also holds Branch Admin → broader
		# scope wins → the group-axis hook returns "" (no narrowing). They manage
		# via Branch Admin, not Leader.
		perms.install_gateway(_dual_role_gateway())
		try:
			assert perms.get_group_scoped_conditions(doctype="Flock Group Member", user="dual@north") == ""
			assert perms.has_group_scope("Flock Group Member", "dual@north") is False
		finally:
			perms.install_gateway(perms.NullPermissionGateway())

	def test_dual_role_passes_group_guard_outside_led_subtree(self):
		# Treated as Branch Admin, the dual-role user passes the group guard even
		# on a group outside any led subtree (broader scope wins for management).
		gw = _dual_role_gateway()
		perms.assert_group_scope(doc_group="G-foreign", doc_member="M-other", user="dual@north", gateway=gw)

	def test_dual_role_branch_scope_still_enforced(self):
		# Broader-scope-wins is about the GROUP axis, not a license to cross
		# branches. The dual-role North admin still cannot touch South.
		gw = _dual_role_gateway()
		with pytest.raises(perms.FlockPermissionError):
			perms.assert_branch_scope(doc_branch="South", user="dual@north", gateway=gw)

	def test_plain_leader_emits_group_fragment(self):
		# The converse assertion: a plain Leader (no Branch Admin) IS group-scoped
		# → the hook returns a non-empty fragment narrowing to their led subtree.
		perms.install_gateway(_plain_leader_gateway())
		try:
			sql = perms.get_group_scoped_conditions(doctype="Flock Group Member", user="lead@north")
			assert sql.startswith(" AND (")
			assert "G-North" in sql or "`tabFlock Group`.`lft` >= 1" in sql
			assert perms.has_group_scope("Flock Group Member", "lead@north") is True
		finally:
			perms.install_gateway(perms.NullPermissionGateway())

	def test_plain_leader_blocked_outside_led_subtree_at_guard(self):
		# The plain Leader cannot act on a group outside their led subtree.
		gw = _plain_leader_gateway()
		with pytest.raises(perms.FlockPermissionError):
			perms.assert_group_scope(
				doc_group="G-foreign", doc_member="M-other", user="lead@north", gateway=gw
			)


# --------------------------------------------------------------------------- #
# §4.7 — null-branch org-wide rows are visible to any authenticated user.
# --------------------------------------------------------------------------- #


class TestOrgWideRows:
	"""Org-wide rows (nullable branch) are not branch-scoped (§4.7 / §6.2)."""

	@pytest.mark.parametrize(
		"roles",
		[
			[perms.ROLE_ORG_ADMIN],
			[perms.ROLE_AUDITOR],
			[perms.ROLE_BRANCH_ADMIN],
			[perms.ROLE_GROUP_LEADER],
			[perms.ROLE_MEMBER],
		],
	)
	def test_null_branch_visible_to_every_role(self, roles):
		# An org announcement (branch=None) is reachable regardless of role — the
		# branch axis simply does not apply to org-wide rows.
		assert perms.can_access_branch(branch=None, allowed_branches=(), roles=roles) is True

	def test_null_branch_passes_guard_for_branch_admin(self):
		gw = _north_branch_admin_gateway()
		# No exception for an org-wide row even though the admin is North-scoped.
		perms.assert_branch_scope(doc_branch=None, user="ba@north", gateway=gw)
