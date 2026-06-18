"""
Project-level tests for :mod:`flock_os.permissions` — the FLO-20 permission layer.

These run under plain ``pytest`` (no Frappe site / bench required). The pure
halves of both scoping axes — the group-axis SQL builder, the branch-axis
subtree materialization, the bypass/scope decisions, and the guards — are
exercised against an in-memory :class:`RecordingPermissionGateway` so every rule
in ADR-0001 §6 is pinned without a database. This is the same hexagonal-port
discipline as :mod:`flock_os.traversal` / :mod:`flock_os.events`.

Coverage map (ADR-0001 §6 / FLO-5 §4):

* §6.2 branch axis — :func:`compute_branch_subtree`, :func:`can_access_branch`.
* §6.3 group axis — :func:`build_group_scope_sql` (OR-fragment, self-predication
  edit #2, live bounds edit #3, self-membership edit #4, bypass, deny-default).
* §6.5 rules of engagement — :func:`assert_branch_scope`, :func:`assert_group_scope`,
  :func:`system_query` (audited escape hatch), :data:`SCOPED_DOCTYPES`.

The Frappe-backed ``permission_query_conditions`` transport + UP syncer are
integration-tested via ``bench run-tests``; the project-level gate asserts the
module loads cleanly without Frappe and the scope contract is correct.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from flock_os import permissions as perms
from flock_os.flock_os import trees
from flock_os.permissions import (
	BYPASS_ROLES,
	GroupBounds,
	LeaderScope,
	PermissionGateway,
)

# --------------------------------------------------------------------------- #
# In-memory gateway — a fully-known two-tree world for deterministic scopes.
# --------------------------------------------------------------------------- #
# Branch tree (a regional admin scope):
#
#   HQ                (Org Admin sees all; no UP)
#    ├── North        (Branch Admin root — sees North + North-A + North-A1)
#    │    ├── North-A
#    │    │    └── North-A1
#    │    └── North-B
#    └── South        (independent branch — isolation target)
#
# Group tree (within "North"; leader M1 leads a subtree, M2 joins a group):
#
#   G0 (lft 1, rgt 8, leader M1)        ← M1 leads the root group
#    ├── G1 (lft 2, rgt 5, leader M1)   ← M1 also leads G1 (leads 1..N)
#    │    └── G2 (lft 3, rgt 4)
#    └── G3 (lft 6, rgt 7)              ← M2 is a plain member here (joined)
#
# M1 therefore leads G0 + G1; M2 leads nothing but joins G3.


def _gb(name: str, lft: int, rgt: int) -> GroupBounds:
	return GroupBounds(name=name, lft=lft, rgt=rgt)


BRANCH_PARENT_OF: dict[str, str | None] = {
	"HQ": None,
	"North": "HQ",
	"North-A": "North",
	"North-A1": "North-A",
	"North-B": "North",
	"South": "HQ",
}
BRANCH_CHILDREN_OF: dict[str, list[str]] = {
	"HQ": ["North", "South"],
	"North": ["North-A", "North-B"],
	"North-A": ["North-A1"],
	"North-A1": [],
	"North-B": [],
	"South": [],
}

# Live nested-set bounds for the group tree above.
GROUP_BOUNDS: dict[str, GroupBounds] = {
	"G0": _gb("G0", 1, 8),
	"G1": _gb("G1", 2, 5),
	"G2": _gb("G2", 3, 4),
	"G3": _gb("G3", 6, 7),
}


@dataclass
class RecordingPermissionGateway(PermissionGateway):
	"""In-memory gateway: deterministic roles / member / led / joined / branches."""

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


def _passthrough(value: Any) -> str:
	"""Escape stand-in: wraps the value in single quotes (DB-safe shape)."""
	return f"'{value}'"


# --------------------------------------------------------------------------- #
# SCOPED_DOCTYPES + role catalog (ADR §6.4 / §6.5)
# --------------------------------------------------------------------------- #


def test_scoped_doctypes_covers_phase1_group_subset():
	# The single list the hook + audit gate consult. Flock Group (self-scoped)
	# + Flock Group Member (the group-level edge) ship in Phase 1.
	assert "Flock Group" in perms.SCOPED_DOCTYPES
	assert "Flock Group Member" in perms.SCOPED_DOCTYPES


def test_bypass_roles_match_adr():
	# §6.3: Org Admin + Auditor see all; Branch Admin scoped natively (not group).
	assert BYPASS_ROLES == frozenset({perms.ROLE_ORG_ADMIN, perms.ROLE_AUDITOR, perms.ROLE_BRANCH_ADMIN})
	# Group-scoped roles are the complement of the Flock leadership/membership set.
	assert perms.ROLE_GROUP_LEADER in perms.GROUP_SCOPED_ROLES
	assert perms.ROLE_MEMBER in perms.GROUP_SCOPED_ROLES


# --------------------------------------------------------------------------- #
# Group-axis SQL builder (ADR §6.3) — the one custom mechanism.
# --------------------------------------------------------------------------- #


def test_leader_fragment_is_or_composed_not_union():
	# §6.3 edit #4: a single OR-fragment appended to WHERE — never a UNION.
	scope = LeaderScope(
		member="M1",
		led_bounds=(_gb("G0", 1, 8),),
		joined_groups=("G3",),
	)
	sql = perms.build_group_scope_sql(doctype="Flock Group Member", scope=scope, escape=_passthrough)
	assert sql.startswith(" AND (")
	assert "UNION" not in sql.upper()
	# Subtree sub-select + self-member + joined all present.
	assert "OR" in sql
	assert "`tabFlock Group Member`.`group` IN (SELECT name FROM `tabFlock Group`" in sql
	assert "`tabFlock Group Member`.`member` = 'M1'" in sql
	assert "`tabFlock Group Member`.`group` IN ('G3')" in sql


def test_disjoint_led_groups_or_compose_per_range():
	# A leader leading two disjoint subtrees must not over-select the gap between
	# them: one (lft, rgt) range per led group, OR-composed.
	scope = LeaderScope(
		member="M1",
		led_bounds=(_gb("G1", 2, 5), _gb("G9", 100, 110)),
		joined_groups=(),
	)
	sql = perms.build_group_scope_sql(doctype="Flock Group Member", scope=scope, escape=_passthrough)
	# Both ranges present, OR-composed inside the sub-select.
	assert "`tabFlock Group`.`lft` >= 2 AND `tabFlock Group`.`rgt` <= 5" in sql
	assert "`tabFlock Group`.`lft` >= 100 AND `tabFlock Group`.`rgt` <= 110" in sql
	assert sql.count(" OR ") >= 1


def test_self_predication_for_flock_group_doctype():
	# §6.3 edit #2 (the regression risk): scoping Flock Group itself must
	# predicate on its OWN nested set, not a `.group` field — else leader
	# scoping silently no-ops on the group list.
	scope = LeaderScope(member="M1", led_bounds=(_gb("G0", 1, 8),), joined_groups=("G3",))
	sql = perms.build_group_scope_sql(doctype="Flock Group", scope=scope, escape=_passthrough)
	assert "`tabFlock Group`.`name` IN (SELECT name FROM `tabFlock Group`" in sql
	assert "`tabFlock Group`.`lft` >= 1 AND `tabFlock Group`.`rgt` <= 8" in sql
	# No `.group` link reference on the self-doctype.
	assert ".`group`" not in sql
	assert "`tabFlock Group`.`name` IN ('G3')" in sql


def test_group_only_doctype_omits_member_clause():
	# FLO-54: Flock Gathering carries `group` but NO `member` column. The group-
	# axis fragment must narrow on the led subtree + joined groups, but MUST NOT
	# emit a `.member = self` clause — that column does not exist, so emitting it
	# would yield invalid SQL on every leader's gathering list. Only DocTypes in
	# MEMBER_ANCHORED_DOCTYPES (rows about a person) get the self-membership
	# branch (Flock Group Member does; Flock Gathering does not).
	assert "Flock Gathering" in perms.SCOPED_DOCTYPES
	assert "Flock Gathering" not in perms.MEMBER_ANCHORED_DOCTYPES
	assert "Flock Group Member" in perms.MEMBER_ANCHORED_DOCTYPES

	scope = LeaderScope(
		member="M1",
		led_bounds=(_gb("G0", 1, 8),),
		joined_groups=("G3",),
	)
	sql = perms.build_group_scope_sql(doctype="Flock Gathering", scope=scope, escape=_passthrough)
	# Led-subtree + joined-group narrowing are present.
	assert "`tabFlock Gathering`.`group` IN (SELECT name FROM `tabFlock Group`" in sql
	assert "`tabFlock Gathering`.`group` IN ('G3')" in sql
	# The self-membership clause is suppressed (no member column on a gathering).
	assert "member" not in sql


def test_empty_scope_returns_no_op_fragment():
	# Bypass roles / users with no resolved member get "" (Frappe composes no-op).
	assert (
		perms.build_group_scope_sql(
			doctype="Flock Group", scope=LeaderScope(member=None), escape=_passthrough
		)
		== ""
	)
	assert (
		perms.build_group_scope_sql(
			doctype="Flock Group Member",
			scope=LeaderScope(member=None, led_bounds=(), joined_groups=()),
			escape=_passthrough,
		)
		== ""
	)


def test_member_with_no_led_groups_sees_only_joined_and_self():
	# A plain member (no leadership) → no subtree branch, only self-member +
	# joined groups. The subtree sub-select matches nothing (WHERE 0).
	scope = LeaderScope(member="M2", led_bounds=(), joined_groups=("G3",))
	sql = perms.build_group_scope_sql(doctype="Flock Group Member", scope=scope, escape=_passthrough)
	assert "SELECT name FROM `tabFlock Group` WHERE 0" in sql
	assert "`tabFlock Group Member`.`member` = 'M2'" in sql
	assert "`tabFlock Group Member`.`group` IN ('G3')" in sql


def test_joined_only_member_on_flock_group_doctype():
	# A non-leader viewing the group list sees only groups they belong to.
	scope = LeaderScope(member="M2", led_bounds=(), joined_groups=("G3",))
	sql = perms.build_group_scope_sql(doctype="Flock Group", scope=scope, escape=_passthrough)
	assert "`tabFlock Group`.`name` IN ('G3')" in sql
	# No subtree range clause (no led bounds).
	assert "`tabFlock Group`.`lft` >=" not in sql


# --------------------------------------------------------------------------- #
# resolve_leader_scope — composes the gateway reads (ADR §6.3).
# --------------------------------------------------------------------------- #


def test_bypass_role_short_circuits_scope_resolution():
	gw = RecordingPermissionGateway(
		roles_by_user={"org@flock": frozenset({perms.ROLE_ORG_ADMIN})},
		member_by_user={"org@flock": "M0"},
		led_bounds_by_member={"M0": (_gb("G0", 1, 8),)},
	)
	scope = perms.resolve_leader_scope(gw, user="org@flock")
	# Bypass → no led bounds (the hook emits no fragment; Frappe sees all).
	assert scope.is_leader is False
	assert scope.led_bounds == ()


def test_leader_scope_resolves_led_and_joined():
	gw = RecordingPermissionGateway(
		roles_by_user={"lead@flock": frozenset({perms.ROLE_GROUP_LEADER})},
		member_by_user={"lead@flock": "M1"},
		led_bounds_by_member={"M1": (_gb("G0", 1, 8), _gb("G1", 2, 5))},
		joined_by_member={"M1": ("G3",)},
	)
	scope = perms.resolve_leader_scope(gw, user="lead@flock")
	assert scope.member == "M1"
	assert scope.is_leader is True
	assert {b.name for b in scope.led_bounds} == {"G0", "G1"}
	assert scope.joined_groups == ("G3",)


def test_user_with_no_linked_member_has_no_group_scope():
	gw = RecordingPermissionGateway(
		roles_by_user={"nobody@flock": frozenset({perms.ROLE_GROUP_LEADER})},
	)
	scope = perms.resolve_leader_scope(gw, user="nobody@flock")
	assert scope.member is None
	assert scope.is_leader is False


# --------------------------------------------------------------------------- #
# permission_query_conditions hook — the transport entry point (ADR §6.3).
# --------------------------------------------------------------------------- #


def test_hook_returns_empty_for_bypass_role():
	gw = RecordingPermissionGateway(
		roles_by_user={"aud@flock": frozenset({perms.ROLE_AUDITOR})},
		member_by_user={"aud@flock": "M9"},
		led_bounds_by_member={"M9": (_gb("G0", 1, 8),)},
	)
	perms.install_gateway(gw)
	try:
		# Auditor → no-op (sees all via DocPerm; not group-scoped).
		assert perms.get_group_scoped_conditions("Flock Group", "aud@flock") == ""
		assert perms.has_group_scope("Flock Group", "aud@flock") is False
	finally:
		perms.install_gateway(perms.NullPermissionGateway())


def test_hook_returns_empty_for_unscoped_doctype():
	# The hook is a no-op for doctypes not in SCOPED_DOCTYPES (defensive).
	perms.install_gateway(perms.NullPermissionGateway())
	try:
		assert perms.get_group_scoped_conditions("Some Other DocType", "x@flock") == ""
	finally:
		perms.install_gateway(perms.NullPermissionGateway())


def test_hook_emits_fragment_for_leader_with_self_predication():
	# The §6.3 regression: a leader viewing Flock Group MUST get a non-empty
	# fragment (self-predication), not a silent no-op.
	gw = RecordingPermissionGateway(
		roles_by_user={"lead@flock": frozenset({perms.ROLE_GROUP_LEADER})},
		member_by_user={"lead@flock": "M1"},
		led_bounds_by_member={"M1": (_gb("G0", 1, 8),)},
	)
	perms.install_gateway(gw)
	try:
		sql = perms.get_group_scoped_conditions("Flock Group", "lead@flock")
		assert sql.startswith(" AND (")
		assert "`tabFlock Group`.`lft` >= 1" in sql
		assert perms.has_group_scope("Flock Group", "lead@flock") is True
	finally:
		perms.install_gateway(perms.NullPermissionGateway())


# --------------------------------------------------------------------------- #
# Branch-axis subtree materialization (ADR §6.2) — pure half.
# --------------------------------------------------------------------------- #


def test_compute_branch_subtree_returns_branch_plus_descendants():
	# A regional admin's allowed set = their branch + every descendant (BFS, so
	# siblings at the same level come before their nieces: North-A and North-B
	# precede North-A1).
	subtree = perms.compute_branch_subtree(
		"North", parent_of=BRANCH_PARENT_OF, children_of=BRANCH_CHILDREN_OF
	)
	assert subtree == ("North", "North-A", "North-B", "North-A1")


def test_compute_branch_subtree_isolates_sibling_branches():
	# Tenant isolation: South is NOT in North's admin subtree.
	north = set(
		perms.compute_branch_subtree("North", parent_of=BRANCH_PARENT_OF, children_of=BRANCH_CHILDREN_OF)
	)
	south = set(
		perms.compute_branch_subtree("South", parent_of=BRANCH_PARENT_OF, children_of=BRANCH_CHILDREN_OF)
	)
	assert north.isdisjoint(south)


def test_compute_branch_subtree_requires_branch():
	with pytest.raises(trees.FlockTreeError):
		perms.compute_branch_subtree("", parent_of=BRANCH_PARENT_OF, children_of=BRANCH_CHILDREN_OF)


def test_rebuild_target_is_the_moved_branches_root():
	# On flock.branch.moved, the broadest scope that might need re-sync is the
	# moved branch's root.
	assert perms.compute_branch_subtree_rebuild_target("North-A1", parent_of=BRANCH_PARENT_OF) == "HQ"


# --------------------------------------------------------------------------- #
# can_access_branch + is_bypass_user (ADR §6.2 bypass rules).
# --------------------------------------------------------------------------- #


def test_bypass_roles_see_all_branches():
	assert perms.is_bypass_user([perms.ROLE_ORG_ADMIN]) is True
	assert perms.is_bypass_user([perms.ROLE_AUDITOR]) is True
	assert perms.is_bypass_user([perms.ROLE_BRANCH_ADMIN]) is True
	assert (
		perms.can_access_branch(branch="South", allowed_branches=("North",), roles=[perms.ROLE_ORG_ADMIN])
		is True
	)


def test_branch_admin_sees_only_allowed_subtree():
	# A North admin (allowed = North subtree) cannot see South.
	allowed = perms.compute_branch_subtree(
		"North", parent_of=BRANCH_PARENT_OF, children_of=BRANCH_CHILDREN_OF
	)
	assert (
		perms.can_access_branch(branch="North-A", allowed_branches=allowed, roles=[perms.ROLE_BRANCH_ADMIN])
		is True
	)
	assert (
		perms.can_access_branch(branch="South", allowed_branches=allowed, roles=[perms.ROLE_BRANCH_ADMIN])
		is False
	)


def test_null_branch_is_visible_to_any_authenticated_user():
	# Org-wide rows (nullable branch, e.g. an org announcement) are not
	# branch-scoped — branch axis does not apply.
	assert perms.can_access_branch(branch=None, allowed_branches=(), roles=[perms.ROLE_MEMBER]) is True


def test_dual_role_broader_scope_wins():
	# §6.3 rec #6: a Leader who is also Branch Admin is treated as Branch Admin.
	roles = [perms.ROLE_GROUP_LEADER, perms.ROLE_BRANCH_ADMIN]
	assert perms.is_bypass_user(roles) is True


# --------------------------------------------------------------------------- #
# Guards — assert_branch_scope / assert_group_scope (ADR §6.5 / §4.7 #3).
# --------------------------------------------------------------------------- #


def test_assert_branch_scope_passes_for_bypass():
	gw = RecordingPermissionGateway(roles_by_user={"org@flock": frozenset({perms.ROLE_ORG_ADMIN})})
	# No exception for any branch.
	perms.assert_branch_scope(doc_branch="South", user="org@flock", gateway=gw)


def test_assert_branch_scope_passes_for_allowed_branch():
	north = perms.compute_branch_subtree("North", parent_of=BRANCH_PARENT_OF, children_of=BRANCH_CHILDREN_OF)
	gw = RecordingPermissionGateway(
		roles_by_user={"ba@flock": frozenset({perms.ROLE_BRANCH_ADMIN})},
		branch_ups_by_user={"ba@flock": north},
	)
	perms.assert_branch_scope(doc_branch="North-A1", user="ba@flock", gateway=gw)


def test_assert_branch_scope_raises_on_cross_branch():
	# Tenant isolation: a North admin touching a South row → FlockPermissionError.
	north = perms.compute_branch_subtree("North", parent_of=BRANCH_PARENT_OF, children_of=BRANCH_CHILDREN_OF)
	gw = RecordingPermissionGateway(
		roles_by_user={"ba@flock": frozenset({perms.ROLE_BRANCH_ADMIN})},
		branch_ups_by_user={"ba@flock": north},
	)
	with pytest.raises(perms.FlockPermissionError):
		perms.assert_branch_scope(doc_branch="South", user="ba@flock", gateway=gw)


def test_assert_group_scope_passes_for_led_subtree():
	gw = RecordingPermissionGateway(
		roles_by_user={"lead@flock": frozenset({perms.ROLE_GROUP_LEADER})},
		member_by_user={"lead@flock": "M1"},
		led_bounds_by_member={"M1": (_gb("G0", 1, 8),)},
		group_bounds_by_name={"G0": _gb("G0", 1, 8)},
	)
	# A row anchored at a group inside the leader's led subtree.
	perms.assert_group_scope(doc_group="G0", doc_member=None, user="lead@flock", gateway=gw)


def test_assert_group_scope_passes_for_descendant_of_led_subtree():
	# ADR §6.3: the guard must agree with the list hook. A leader of G0 (subtree
	# ⊇ G1, G2) sees a row anchored at descendant G2 via the nested-set hook, so
	# the guard must also pass for G2 — not raise as a direct-membership check
	# would (G2 ∉ {G0}). This is the consistency gap between guard and hook.
	gw = RecordingPermissionGateway(
		roles_by_user={"lead@flock": frozenset({perms.ROLE_GROUP_LEADER})},
		member_by_user={"lead@flock": "M1"},
		led_bounds_by_member={"M1": (_gb("G0", 1, 8),)},
		group_bounds_by_name={"G2": _gb("G2", 3, 4)},
	)
	perms.assert_group_scope(doc_group="G2", doc_member=None, user="lead@flock", gateway=gw)


def test_assert_group_scope_passes_for_joined_group():
	# §4.1: a plain member may act on a row anchored at a group they belong to.
	gw = RecordingPermissionGateway(
		roles_by_user={"mem@flock": frozenset({perms.ROLE_MEMBER})},
		member_by_user={"mem@flock": "M2"},
		joined_by_member={"M2": ("G3",)},
		group_bounds_by_name={"G3": _gb("G3", 6, 7)},
	)
	perms.assert_group_scope(doc_group="G3", doc_member=None, user="mem@flock", gateway=gw)


def test_assert_group_scope_passes_for_self_member():
	# §6.3 edit #4: self-membership exception.
	gw = RecordingPermissionGateway(
		roles_by_user={"mem@flock": frozenset({perms.ROLE_MEMBER})},
		member_by_user={"mem@flock": "M2"},
		joined_by_member={"M2": ("G3",)},
	)
	perms.assert_group_scope(doc_group=None, doc_member="M2", user="mem@flock", gateway=gw)


def test_assert_group_scope_raises_outside_led_subtree():
	gw = RecordingPermissionGateway(
		roles_by_user={"lead@flock": frozenset({perms.ROLE_GROUP_LEADER})},
		member_by_user={"lead@flock": "M1"},
		led_bounds_by_member={"M1": (_gb("G0", 1, 8),)},
	)
	with pytest.raises(perms.FlockPermissionError):
		# G-foreign is not in M1's led subtree nor joined set nor self.
		perms.assert_group_scope(doc_group="G-foreign", doc_member="M-other", user="lead@flock", gateway=gw)


def test_assert_group_scope_bypass_for_dual_role():
	# Broader scope wins: a Branch Admin passes even outside any led group.
	gw = RecordingPermissionGateway(
		roles_by_user={"ba@flock": frozenset({perms.ROLE_BRANCH_ADMIN})},
	)
	perms.assert_group_scope(doc_group="G-foreign", doc_member="M-other", user="ba@flock", gateway=gw)


# --------------------------------------------------------------------------- #
# system_query — the audited cross-scope escape hatch (ADR §6.5 / §4.7 #2).
# --------------------------------------------------------------------------- #


@dataclass
class RecordingSystemQueryGateway(perms.SystemQueryGateway):
	reads: list[tuple[str, dict[str, Any], list[str]]] = field(default_factory=list)
	audits: list[dict[str, str]] = field(default_factory=list)

	def system_get_all(self, doctype, *, filters, fields):
		self.reads.append((doctype, dict(filters), list(fields)))
		return [{"name": "row-1"}]

	def audit(self, *, action, doctype, actor, reason, detail):
		self.audits.append(
			{"action": action, "doctype": doctype, "actor": actor, "reason": reason, "detail": detail}
		)


def test_system_query_bypasses_and_audits():
	gw = RecordingSystemQueryGateway()
	results = perms.system_query(
		gw,
		doctype="Flock Member",
		filters={"branch": "North"},
		fields=["name", "email"],
		reason="quarterly compliance export",
		actor="auditor@flock",
	)
	assert results == [{"name": "row-1"}]
	# The bypassing read was captured exactly once with the requested scope.
	assert gw.reads == [("Flock Member", {"branch": "North"}, ["name", "email"])]
	# The audit row was written with the mandatory reason.
	assert len(gw.audits) == 1
	assert gw.audits[0]["action"] == "system_query"
	assert gw.audits[0]["reason"] == "quarterly compliance export"
	assert gw.audits[0]["actor"] == "auditor@flock"


def test_null_gateway_is_import_clean_without_frappe():
	# The default gateway yields no scope; import never touches Frappe.
	null = perms.NullPermissionGateway()
	assert null.get_user_roles("anyone") == frozenset()
	assert null.resolve_member_for_user("anyone") is None
	assert null.fetch_led_group_bounds("M1") == ()


# --------------------------------------------------------------------------- #
# Flock Gathering — group-axis scoping (FLO-54 / FLO-6 §6). The gathering is a
# group-level transactional DocType; it must be registered in SCOPED_DOCTYPES so
# the single `permission_query_conditions` hook narrows a leader to their led
# subtree (a leader can read/create gatherings only for groups they lead), while
# bypass roles (Org/Branch Admin, Auditor) see all. These pin the DoD: "leader
# can create/read gatherings in-scope only".
# --------------------------------------------------------------------------- #


def test_flock_gathering_is_registered_as_group_scoped():
	# The hook + audit gate consult SCOPED_DOCTYPES; the gathering must be in it.
	assert "Flock Gathering" in perms.SCOPED_DOCTYPES


def test_flock_gathering_hook_narrows_leader_to_led_subtree():
	# A leader (M1 leads G0, lft/rgt 1..8) viewing the gathering list gets a
	# non-empty OR-fragment that predicates on `.group` IN the leader's led
	# subtree — i.e. they only see gatherings anchored under a group they lead.
	gw = RecordingPermissionGateway(
		roles_by_user={"lead@flock": frozenset({perms.ROLE_GROUP_LEADER})},
		member_by_user={"lead@flock": "M1"},
		led_bounds_by_member={"M1": (_gb("G0", 1, 8),)},
	)
	perms.install_gateway(gw)
	try:
		sql = perms.get_group_scoped_conditions("Flock Gathering", "lead@flock")
		assert sql.startswith(" AND (")
		# The gathering scopes via its `.group` link (not self-predication).
		assert "`tabFlock Gathering`.`group` IN (SELECT name FROM `tabFlock Group`" in sql
		assert "`tabFlock Group`.`lft` >= 1 AND `tabFlock Group`.`rgt` <= 8" in sql
		assert perms.has_group_scope("Flock Gathering", "lead@flock") is True
	finally:
		perms.install_gateway(perms.NullPermissionGateway())


def test_flock_gathering_hook_is_noop_for_bypass_role():
	# A Branch/Org Admin / Auditor sees all gatherings (no group-axis fragment).
	gw = RecordingPermissionGateway(
		roles_by_user={"ba@flock": frozenset({perms.ROLE_BRANCH_ADMIN})},
		member_by_user={"ba@flock": "M9"},
		led_bounds_by_member={"M9": (_gb("G0", 1, 8),)},
	)
	perms.install_gateway(gw)
	try:
		assert perms.get_group_scoped_conditions("Flock Gathering", "ba@flock") == ""
		assert perms.has_group_scope("Flock Gathering", "ba@flock") is False
	finally:
		perms.install_gateway(perms.NullPermissionGateway())


def test_flock_gathering_assert_group_scope_uses_subtree_containment():
	# The write/create guard agrees with the list hook: a leader of G0 may act
	# on a gathering anchored at descendant G2, but not at a foreign group.
	gw = RecordingPermissionGateway(
		roles_by_user={"lead@flock": frozenset({perms.ROLE_GROUP_LEADER})},
		member_by_user={"lead@flock": "M1"},
		led_bounds_by_member={"M1": (_gb("G0", 1, 8),)},
		group_bounds_by_name={"G2": _gb("G2", 3, 4), "G-foreign": _gb("G-foreign", 100, 101)},
	)
	# Descendant of the led subtree → allowed.
	perms.assert_group_scope(doc_group="G2", doc_member=None, user="lead@flock", gateway=gw)
	# Foreign group → denied.
	with pytest.raises(perms.FlockPermissionError):
		perms.assert_group_scope(doc_group="G-foreign", doc_member=None, user="lead@flock", gateway=gw)
