"""
Permission-matrix integration tests — role × document × row-level org-tree-node.

Decomposed from [FLO-221](/FLO/issues/FLO-221) deliverable #2 (P5.1a). This is
the **pure-backend** slice: it exercises the real
:mod:`flock_os.permissions` scoping chokepoint (ADR-0001 §6 — resolve → scope →
guard) end-to-end via the in-memory :class:`RecordingPermissionGateway`, over a
deterministic seeded multi-branch tree. No Frappe bench (project SQLite-fast
gate) — identical harness discipline to :mod:`flock_os.tests.test_permissions`
and :mod:`scripts.demo_phase1`, so the two stay DRY (no forked harness).

Matrix axes (the full enterprise-perm confinement proof):

* **Roles** — every role in the ``FLOCK_ROLES`` catalog mirrored from
  :mod:`flock_os.permissions` (Org Admin, Branch Admin, Auditor, Group Leader,
  Member, Visitor).
* **Documents** — every entry in :data:`permissions.SCOPED_DOCTYPES` (the single
  list the ``permission_query_conditions`` hook narrows), grouped by kind:
  self-predication (``Flock Group``), member-anchored
  (:data:`permissions.MEMBER_ANCHORED_DOCTYPES` — the self-membership special
  case), and group-only (no ``member`` column → self clause suppressed, FLO-54).
* **Row positions** — own-subtree, descendant-of-led, joined-group, self-member,
  cross-subtree-foreign (same branch, foreign subtree), cross-branch.

Assertions (the DoD):

1. A leader/member sees **only** their targetable subtree + joined + self rows;
   cross-subtree and cross-branch reads are narrowed out by the hook fragment
   AND denied by the guard.
2. Forged/escalated requests raise :class:`FlockPermissionError` at the guard
   (:func:`assert_group_scope` / :func:`assert_branch_scope`) — including a
   non-chain leader attempting an approval step (:func:`assert_approval_scope`).
3. Bypass roles (Org Admin / Auditor see all; Branch Admin their branch subtree)
   emit no group-axis fragment and pass the guards; dual-role broader-scope-wins.
4. The guard and the list hook **agree** (subtree-containment parity, ADR §6.3) —
   asserted cell-by-cell across the whole matrix via a pure inclusion helper
   that mirrors the fragment's predicate, then cross-checked against the real
   :func:`assert_group_scope`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from flock_os import permissions as perms
from flock_os.approvals import LEVEL_BRANCH_ADMIN, LEVEL_PARENT
from flock_os.permissions import (
	GroupBounds,
	LeaderScope,
	PermissionGateway,
)

# --------------------------------------------------------------------------- #
# Seeded multi-branch world (DRY with scripts/demo_phase1.py — same shape).
#
# Branch tree (the administrative / branch axis):
#
#   Flock HQ                         (root org — Org Admin / Auditor see all)
#   ├── North                        (Branch Admin root — sees North subtree)
#   │   ├── North-Campus
#   │   │   └── North-Outpost        (nested under another branch)
#   │   └── North-East
#   └── South                        (independent tenant — isolation target)
#       └── South-Campus
#
# Group tree (branch-bound — every group subtree lives inside one branch). The
# nested-set bounds are globally non-overlapping (one lft/rgt space per tree
# doctype), so pure-numeric containment can never cross branch lines:
#
#   North-Ministries (North, leader M-Lead)        lft 1  rgt 8
#   ├── Worship     (North, leader M-Lead)         lft 2  rgt 3
#   └── Youth       (North, leader M-Lead)         lft 4  rgt 7
#        └── Youth-Band (North, leader M-Other)    lft 5  rgt 6
#   North-Service   (North, leader M-North-Svc)    lft 13 rgt 14   ← same-branch
#                                                                  sibling root,
#                                                                  FOREIGN to
#                                                                  M-Lead's subtree
#   South-Ministries (South, leader M-South-Lead)  lft 9  rgt 12
#   └── Outreach     (South, leader M-South-Lead)  lft 10 rgt 11
#
# ``North-Service`` is the one deliberate extension over demo_phase1's tree: a
# same-branch (North) group that is NOT under M-Lead's led subtree, so the
# "cross-subtree-foreign" matrix axis (distinct from cross-branch) is covered.
# --------------------------------------------------------------------------- #

BRANCH_PARENT_OF: dict[str, str | None] = {
	"Flock HQ": None,
	"North": "Flock HQ",
	"North-Campus": "North",
	"North-Outpost": "North-Campus",
	"North-East": "North",
	"South": "Flock HQ",
	"South-Campus": "South",
}
BRANCH_CHILDREN_OF: dict[str, list[str]] = {
	"Flock HQ": ["North", "South"],
	"North": ["North-Campus", "North-East"],
	"North-Campus": ["North-Outpost"],
	"North-Outpost": [],
	"North-East": [],
	"South": ["South-Campus"],
	"South-Campus": [],
}
ALL_BRANCHES: tuple[str, ...] = tuple(BRANCH_PARENT_OF)

NORTH_SUBTREE: tuple[str, ...] = perms.compute_branch_subtree(
	"North", parent_of=BRANCH_PARENT_OF, children_of=BRANCH_CHILDREN_OF
)
SOUTH_SUBTREE: tuple[str, ...] = perms.compute_branch_subtree(
	"South", parent_of=BRANCH_PARENT_OF, children_of=BRANCH_CHILDREN_OF
)


def _gb(name: str, lft: int, rgt: int) -> GroupBounds:
	return GroupBounds(name=name, lft=lft, rgt=rgt)


# One globally-non-overlapping nested set over the Flock Group doctype.
GROUP_BOUNDS: dict[str, GroupBounds] = {
	"North-Ministries": _gb("North-Ministries", 1, 8),
	"Worship": _gb("Worship", 2, 3),
	"Youth": _gb("Youth", 4, 7),
	"Youth-Band": _gb("Youth-Band", 5, 6),
	"South-Ministries": _gb("South-Ministries", 9, 12),
	"Outreach": _gb("Outreach", 10, 11),
	"North-Service": _gb("North-Service", 13, 14),
}


# --------------------------------------------------------------------------- #
# RecordingPermissionGateway — the same hexagonal in-memory port as
# test_permissions.py / demo_phase1.py. No forked harness: dict-backed fields
# implementing PermissionGateway, deterministic per-actor scope.
# --------------------------------------------------------------------------- #
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
# Representative actors — one per role in the FLOCK_ROLES catalog. Each actor's
# resolved scope exercises a distinct branch of the resolve→scope→guard logic.
# --------------------------------------------------------------------------- #
ORG_ADMIN_USER = "admin@flock"
BRANCH_ADMIN_USER = "ba@north"
AUDITOR_USER = "auditor@flock"
GROUP_LEADER_USER = "lead@north"
MEMBER_USER = "member@north"
VISITOR_USER = "visitor@north"

NORTH_LEADER_BOUNDS: tuple[GroupBounds, ...] = (GROUP_BOUNDS["North-Ministries"],)


def _make_gateway() -> RecordingPermissionGateway:
	"""Build the seeded multi-branch world with all six role actors populated."""
	return RecordingPermissionGateway(
		roles_by_user={
			ORG_ADMIN_USER: frozenset({perms.ROLE_ORG_ADMIN}),
			BRANCH_ADMIN_USER: frozenset({perms.ROLE_BRANCH_ADMIN}),
			AUDITOR_USER: frozenset({perms.ROLE_AUDITOR}),
			GROUP_LEADER_USER: frozenset({perms.ROLE_GROUP_LEADER}),
			MEMBER_USER: frozenset({perms.ROLE_MEMBER}),
			VISITOR_USER: frozenset({perms.ROLE_VISITOR}),
		},
		member_by_user={
			ORG_ADMIN_USER: "M-Admin",
			BRANCH_ADMIN_USER: "M-BA-North",
			AUDITOR_USER: "M-Auditor",
			GROUP_LEADER_USER: "M-Lead",
			MEMBER_USER: "M-Member",
			VISITOR_USER: "M-Visitor",
		},
		led_bounds_by_member={
			# M-Lead leads the North-Ministries root → subtree ⊇ Worship, Youth,
			# Youth-Band.
			"M-Lead": NORTH_LEADER_BOUNDS,
		},
		joined_by_member={
			# A plain member joins one group they do NOT lead.
			"M-Member": ("Worship",),
		},
		branch_ups_by_user={
			# Branch Admin carries its materialized North subtree (§6.2).
			BRANCH_ADMIN_USER: NORTH_SUBTREE,
			# Group-scoped roles carry their home branch UP (branch-axis isolation).
			GROUP_LEADER_USER: ("North",),
			MEMBER_USER: ("North",),
			VISITOR_USER: ("North",),
		},
		group_bounds_by_name=dict(GROUP_BOUNDS),
	)


# --------------------------------------------------------------------------- #
# Doctype grouping — derived from the canonical lists (DRY, single source).
# --------------------------------------------------------------------------- #
SELF_PREDICATION_DOCTYPES: tuple[str, ...] = (perms.GROUP_DOCTYPE,)
MEMBER_ANCHORED_DOCTYPES: tuple[str, ...] = tuple(perms.MEMBER_ANCHORED_DOCTYPES)
GROUP_ONLY_DOCTYPES: tuple[str, ...] = tuple(
	d for d in perms.SCOPED_DOCTYPES if d != perms.GROUP_DOCTYPE and d not in perms.MEMBER_ANCHORED_DOCTYPES
)

#: All scoped doctypes, each tagged with its kind so the matrix can parametrize
#: uniformly and branch the visibility predicate on kind.
DOCTYPE_KINDS: dict[str, str] = {}
for _d in perms.SCOPED_DOCTYPES:
	if _d == perms.GROUP_DOCTYPE:
		DOCTYPE_KINDS[_d] = "self_predication"
	elif _d in perms.MEMBER_ANCHORED_DOCTYPES:
		DOCTYPE_KINDS[_d] = "member_anchored"
	else:
		DOCTYPE_KINDS[_d] = "group_only"


# --------------------------------------------------------------------------- #
# Row positions — the six org-tree-node positions the matrix exercises.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RowPosition:
	"""A concrete row anchor (group + member) at a named tree position."""

	name: str
	group: str | None
	member: str | None


ROW_POSITIONS: tuple[RowPosition, ...] = (
	RowPosition("own_subtree", "North-Ministries", "M-Lead"),
	RowPosition("descendant_of_led", "Youth-Band", "M-Other"),
	RowPosition("joined_group", "Worship", "M-Worshipper"),
	RowPosition("self_member", "Youth", "M-Member"),
	RowPosition("cross_subtree_foreign", "North-Service", "M-North-Svc"),
	RowPosition("cross_branch", "Outreach", "M-South-Lead"),
)


# --------------------------------------------------------------------------- #
# Pure inclusion helpers — mirror the hook fragment + guard predicates so the
# "guard and list hook agree" parity (ADR §6.3) is asserted cell-by-cell.
# --------------------------------------------------------------------------- #
def _in_led_subtree(group: str, led_bounds: tuple[GroupBounds, ...]) -> bool:
	"""True iff ``group``'s nested-set bounds sit inside any led bound."""
	gb = GROUP_BOUNDS.get(group)
	if gb is None:
		return False
	return any(led.lft <= gb.lft and gb.rgt <= led.rgt for led in led_bounds)


def _hook_includes(*, scope: LeaderScope, doctype: str, group: str | None, member: str | None) -> bool:
	"""Mirror :func:`build_group_scope_sql`'s row-inclusion predicate.

	For non-null groups this is exactly the fragment's OR-branches: subtree
	containment OR joined OR (member-anchored only) self-membership. The NULL-
	group passthrough (``group IS NULL``) is covered by a dedicated test rather
	than this helper, since the group-axis guard intentionally delegates NULL-
	group rows to the branch axis (no guard parity claimed there).
	"""
	if group is None:
		return True  # the hook's `group IS NULL` passthrough (branch-axis-owned)
	if _in_led_subtree(group, scope.led_bounds):
		return True
	if group in set(scope.joined_groups):
		return True
	if (
		doctype in MEMBER_ANCHORED_DOCTYPES
		and scope.member is not None
		and member is not None
		and member == scope.member
	):
		return True
	return False


def _guard_allows(gateway: PermissionGateway, *, user: str, group: str | None, member: str | None) -> bool:
	"""Run the real :func:`assert_group_scope`; return True iff it passes."""
	try:
		perms.assert_group_scope(doc_group=group, doc_member=member, user=user, gateway=gateway)
	except perms.FlockPermissionError:
		return False
	return True


# The group-scoped roles + their representative users (parametrize id).
GROUP_SCOPED_ACTORS: tuple[tuple[str, str], ...] = (
	("Group Leader", GROUP_LEADER_USER),
	("Member", MEMBER_USER),
	("Visitor", VISITOR_USER),
)


# --------------------------------------------------------------------------- #
# Matrix dimension sanity — the full FLOCK_ROLES × SCOPED_DOCTYPES catalog.
# --------------------------------------------------------------------------- #
def test_matrix_covers_full_role_catalog():
	# The matrix exercises every role in the permission role catalog (ADR §6.4).
	role_catalog = {
		perms.ROLE_ORG_ADMIN,
		perms.ROLE_BRANCH_ADMIN,
		perms.ROLE_AUDITOR,
		perms.ROLE_GROUP_LEADER,
		perms.ROLE_MEMBER,
		perms.ROLE_VISITOR,
	}
	assert role_catalog <= {r for r in perms.BYPASS_ROLES} | set(perms.GROUP_SCOPED_ROLES)
	# Each role is represented by exactly one seeded actor.
	gw = _make_gateway()
	represented = {next(iter(gw.roles_by_user[u])) for u in gw.roles_by_user}
	assert represented == role_catalog


def test_matrix_covers_every_scoped_doctype_with_correct_kind():
	# Every SCOPED_DOCTYPES entry is in the matrix and tagged with its kind.
	assert set(DOCTYPE_KINDS) == set(perms.SCOPED_DOCTYPES)
	# Self-predication is Flock Group alone; member-anchored matches the canonical
	# map; the rest are group-only (no member column → self clause suppressed).
	assert SELF_PREDICATION_DOCTYPES == ("Flock Group",)
	assert set(MEMBER_ANCHORED_DOCTYPES) == set(perms.MEMBER_ANCHORED_DOCTYPES)
	for doctype in MEMBER_ANCHORED_DOCTYPES:
		assert DOCTYPE_KINDS[doctype] == "member_anchored"
	for doctype in GROUP_ONLY_DOCTYPES:
		assert DOCTYPE_KINDS[doctype] == "group_only"
		assert doctype not in perms.MEMBER_ANCHORED_DOCTYPES


def test_seed_tree_is_branch_bound_and_non_overlapping():
	# Containment can never cross branch lines: North and South group subtrees
	# occupy disjoint lft/rgt ranges, and every nested-set child stays inside its
	# parent's range (the structural invariant ADR §4.2 relies on).
	for child, parent in (
		("Worship", "North-Ministries"),
		("Youth", "North-Ministries"),
		("Youth-Band", "Youth"),
		("Outreach", "South-Ministries"),
	):
		c, p = GROUP_BOUNDS[child], GROUP_BOUNDS[parent]
		assert p.lft < c.lft < c.rgt < p.rgt
	north_root = GROUP_BOUNDS["North-Ministries"]
	south_root = GROUP_BOUNDS["South-Ministries"]
	assert north_root.rgt < south_root.lft  # disjoint subtrees


# --------------------------------------------------------------------------- #
# Bypass roles — Org Admin / Auditor emit no group-axis fragment and pass every
# guard; Branch Admin is group-bypass but branch-scoped to its subtree.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("role,user", [("Org Admin", ORG_ADMIN_USER), ("Auditor", AUDITOR_USER)])
def test_global_bypass_roles_emit_no_group_fragment_and_see_all_branches(role, user):
	gw = _make_gateway()
	perms.install_gateway(gw)
	try:
		# No group-axis narrowing for any scoped doctype (bypass → "" fragment).
		for doctype in perms.SCOPED_DOCTYPES:
			assert perms.get_group_scoped_conditions(doctype=doctype, user=user) == ""
			assert perms.has_group_scope(doctype, user) is False
		# Branch axis: these are global-branch roles → every branch visible.
		for branch in ALL_BRANCHES:
			perms.assert_branch_scope(doc_branch=branch, user=user, gateway=gw)
		# Group guard: bypass passes even for a foreign / cross-branch group.
		perms.assert_group_scope(doc_group="Outreach", doc_member="M-South-Lead", user=user, gateway=gw)
	finally:
		perms.install_gateway(perms.NullPermissionGateway())


def test_branch_admin_is_group_bypass_but_branch_scoped_to_north_subtree():
	gw = _make_gateway()
	perms.install_gateway(gw)
	try:
		# §6.3 broader-scope-wins: Branch Admin is NOT group-scoped.
		assert perms.get_group_scoped_conditions(doctype="Flock Group", user=BRANCH_ADMIN_USER) == ""
		# Group guard passes for any group (group axis bypassed).
		perms.assert_group_scope(
			doc_group="Outreach", doc_member="M-South-Lead", user=BRANCH_ADMIN_USER, gateway=gw
		)
		# Branch axis: every North-descendant row reachable …
		for branch in NORTH_SUBTREE:
			perms.assert_branch_scope(doc_branch=branch, user=BRANCH_ADMIN_USER, gateway=gw)
		# … and the nested descendant is in scope (scope grows down, not up).
		assert "North-Outpost" in NORTH_SUBTREE
		# … but a cross-branch (South) row is denied at the branch guard.
		with pytest.raises(perms.FlockPermissionError):
			perms.assert_branch_scope(doc_branch="South", user=BRANCH_ADMIN_USER, gateway=gw)
		with pytest.raises(perms.FlockPermissionError):
			perms.assert_branch_scope(doc_branch="South-Campus", user=BRANCH_ADMIN_USER, gateway=gw)
		# Null-branch rows (org-wide) are visible to any authenticated user.
		perms.assert_branch_scope(doc_branch=None, user=BRANCH_ADMIN_USER, gateway=gw)
	finally:
		perms.install_gateway(perms.NullPermissionGateway())


def test_branch_admin_subtree_isolates_sibling_branches():
	# Tenant isolation: North and South admin subtrees are disjoint.
	assert set(NORTH_SUBTREE).isdisjoint(SOUTH_SUBTREE)
	assert "South" not in NORTH_SUBTREE
	assert "North" not in SOUTH_SUBTREE


# --------------------------------------------------------------------------- #
# Group-scoped roles — the heart of the matrix. For every (role, doctype,
# row-position) cell, the hook fragment's inclusion predicate MUST agree with
# the real guard, and confinement holds: foreign / cross-branch rows excluded.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("role,user", GROUP_SCOPED_ACTORS)
@pytest.mark.parametrize("doctype", list(perms.SCOPED_DOCTYPES))
@pytest.mark.parametrize("position", ROW_POSITIONS, ids=[p.name for p in ROW_POSITIONS])
def test_guard_and_hook_agree_across_role_doctype_position(role, user, doctype, position):
	"""ADR §6.3 parity: the list hook and the write guard implement the same
	subtree-containment predicate. Asserted cell-by-cell across the whole matrix.

	Row shape is doctype-aware: only member-anchored DocTypes carry a ``member``
	column (a Gathering row has a group but no member anchor), so the member
	anchor is passed to the guard / hook only for those DocTypes. The guard's
	self-membership branch (``doc_member == self``) therefore only fires where a
	member column exists — matching exactly where the hook emits ``.member = self``.
	"""
	gw = _make_gateway()
	scope = perms.resolve_leader_scope(gw, user=user)
	# Only member-anchored rows carry a member anchor; group-only / self-
	# predication rows have just a group. Passing a member to the guard for a
	# group-only doctype would be unrealistic (no such column on the row).
	row_member = position.member if doctype in MEMBER_ANCHORED_DOCTYPES else None
	hook_visible = _hook_includes(scope=scope, doctype=doctype, group=position.group, member=row_member)
	guard_visible = _guard_allows(gw, user=user, group=position.group, member=row_member)
	# The guard and the hook MUST agree for every non-null-group cell.
	assert hook_visible == guard_visible, (
		f"parity break {role}/{doctype}/{position.name}: hook={hook_visible} guard={guard_visible}"
	)


@pytest.mark.parametrize("role,user", GROUP_SCOPED_ACTORS)
@pytest.mark.parametrize("doctype", list(perms.SCOPED_DOCTYPES))
def test_group_scoped_role_emits_fragment_for_every_scoped_doctype(role, user, doctype):
	# Every group-scoped role has a linked member → the hook emits a non-empty
	# fragment for every scoped doctype (the §6.3 self-predication regression
	# risk: leader/member narrowing must actually fire, never silently no-op).
	gw = _make_gateway()
	perms.install_gateway(gw)
	try:
		assert perms.has_group_scope(doctype, user) is True
		sql = perms.get_group_scoped_conditions(doctype=doctype, user=user)
		assert sql.startswith(" AND (")
	finally:
		perms.install_gateway(perms.NullPermissionGateway())


@pytest.mark.parametrize("role,user", GROUP_SCOPED_ACTORS)
@pytest.mark.parametrize("doctype", list(MEMBER_ANCHORED_DOCTYPES))
def test_member_anchored_doctype_fragment_emits_self_clause(role, user, doctype):
	# Rows about a person (member-anchored) get the self-membership branch
	# (``.<member-col> = <self>``), keyed on the exact column name per doctype.
	gw = _make_gateway()
	scope = perms.resolve_leader_scope(gw, user=user)
	sql = perms.build_group_scope_sql(doctype=doctype, scope=scope, escape=_passthrough)
	member_col = perms.MEMBER_ANCHORED_DOCTYPES[doctype]
	assert f"`tab{doctype}`.`{member_col}` = " in sql
	# The subtree + joined branches are present alongside the self branch.
	assert f"`tab{doctype}`.`group` IN (SELECT name FROM `tabFlock Group`" in sql
	assert f"`tab{doctype}`.`group` IS NULL" in sql  # passthrough (nullable group)


@pytest.mark.parametrize("doctype", list(GROUP_ONLY_DOCTYPES))
def test_group_only_doctype_fragment_omits_self_clause(doctype):
	# FLO-54: group-only DocTypes (no member column) MUST NOT emit a ``.member``
	# clause — doing so would yield invalid SQL on every leader's list view.
	scope = LeaderScope(
		member="M-Lead",
		led_bounds=NORTH_LEADER_BOUNDS,
		joined_groups=("Worship",),
	)
	sql = perms.build_group_scope_sql(doctype=doctype, scope=scope, escape=_passthrough)
	assert f"`tab{doctype}`.`group` IN (SELECT name FROM `tabFlock Group`" in sql
	# Joined-group narrowing present, self-membership clause suppressed.
	assert f"`tab{doctype}`.`group` IN ('Worship')" in sql
	assert "member" not in sql
	assert ".`member`" not in sql and "registrant" not in sql and "invitee" not in sql


def test_flock_group_fragment_uses_self_predication():
	# §6.3 edit #2: scoping Flock Group predicates on its OWN nested set, never a
	# ``.group`` link reference (else leader scoping silently no-ops on the list).
	scope = LeaderScope(member="M-Lead", led_bounds=NORTH_LEADER_BOUNDS, joined_groups=("Worship",))
	sql = perms.build_group_scope_sql(doctype="Flock Group", scope=scope, escape=_passthrough)
	assert "`tabFlock Group`.`name` IN (SELECT name FROM `tabFlock Group`" in sql
	assert "`tabFlock Group`.`lft` >= 1 AND `tabFlock Group`.`rgt` <= 8" in sql
	assert ".`group`" not in sql


# --------------------------------------------------------------------------- #
# Confinement — the deny-default proof. A leader/member MUST NOT see foreign or
# cross-branch rows: the guard raises, and the hook fragment excludes them.
# --------------------------------------------------------------------------- #
def test_group_leader_sees_only_led_subtree_plus_joined():
	gw = _make_gateway()
	scope = perms.resolve_leader_scope(gw, user=GROUP_LEADER_USER)
	# Leader leads North-Ministries (1..8) → subtree ⊇ Worship, Youth, Youth-Band.
	assert {b.name for b in scope.led_bounds} == {"North-Ministries"}
	for in_scope in ("North-Ministries", "Worship", "Youth", "Youth-Band"):
		assert _in_led_subtree(in_scope, scope.led_bounds) is True
	# Visible via subtree (own / descendant / joined-which-is-also-subtree).
	for visible_pos in ("own_subtree", "descendant_of_led", "joined_group", "self_member"):
		pos = next(p for p in ROW_POSITIONS if p.name == visible_pos)
		assert _hook_includes(scope=scope, doctype="Flock Group Member", group=pos.group, member=pos.member)
		assert _guard_allows(gw, user=GROUP_LEADER_USER, group=pos.group, member=pos.member)
	# Foreign (same-branch sibling) + cross-branch are narrowed out AND denied.
	for denied_pos in ("cross_subtree_foreign", "cross_branch"):
		pos = next(p for p in ROW_POSITIONS if p.name == denied_pos)
		assert not _hook_includes(
			scope=scope, doctype="Flock Group Member", group=pos.group, member=pos.member
		)
		assert not _guard_allows(gw, user=GROUP_LEADER_USER, group=pos.group, member=pos.member)


def test_member_sees_only_joined_plus_self_rows():
	gw = _make_gateway()
	scope = perms.resolve_leader_scope(gw, user=MEMBER_USER)
	# Member leads nothing, joins Worship.
	assert scope.led_bounds == ()
	assert scope.joined_groups == ("Worship",)
	# Joined-group row visible.
	joined = next(p for p in ROW_POSITIONS if p.name == "joined_group")
	assert _hook_includes(scope=scope, doctype="Flock Group Member", group=joined.group, member=joined.member)
	# Self-member row visible ONLY on member-anchored doctypes (the self branch).
	self_pos = next(p for p in ROW_POSITIONS if p.name == "self_member")
	assert _hook_includes(
		scope=scope, doctype="Flock Attendance Record", group=self_pos.group, member=self_pos.member
	)
	# … but NOT on group-only doctypes (no self branch there).
	assert not _hook_includes(
		scope=scope, doctype="Flock Gathering", group=self_pos.group, member=self_pos.member
	)
	# Own-subtree / descendant / foreign / cross-branch all invisible to a member.
	for invisible in ("own_subtree", "descendant_of_led", "cross_subtree_foreign", "cross_branch"):
		pos = next(p for p in ROW_POSITIONS if p.name == invisible)
		assert not _hook_includes(
			scope=scope, doctype="Flock Group Member", group=pos.group, member=pos.member
		)


def test_visitor_with_no_led_or_joined_sees_only_own_member_rows():
	gw = _make_gateway()
	scope = perms.resolve_leader_scope(gw, user=VISITOR_USER)
	assert scope.led_bounds == ()
	assert scope.joined_groups == ()
	# No group-scoped row from the fixed positions is visible to a visitor …
	for pos in ROW_POSITIONS:
		assert not _hook_includes(
			scope=scope, doctype="Flock Group Member", group=pos.group, member=pos.member
		)
	# … but their own member-anchored rows are (self-membership still holds).
	assert _hook_includes(scope=scope, doctype="Flock Attendance Record", group="Youth", member="M-Visitor")
	# … and group-only doctypes show them nothing (no self branch, no joined).
	assert not _hook_includes(scope=scope, doctype="Flock Gathering", group="Youth", member="M-Visitor")


@pytest.mark.parametrize(
	"position_name", ["cross_subtree_foreign", "cross_branch"], ids=["foreign_subtree", "cross_branch"]
)
def test_forged_or_escalated_group_access_denied_at_guard(position_name):
	# Forged/escalated requests raise FlockPermissionError at the group guard for
	# every group-scoped role — the deny-default that makes isolation real.
	pos = next(p for p in ROW_POSITIONS if p.name == position_name)
	gw = _make_gateway()
	for _, user in GROUP_SCOPED_ACTORS:
		with pytest.raises(perms.FlockPermissionError):
			perms.assert_group_scope(doc_group=pos.group, doc_member=pos.member, user=user, gateway=gw)


def test_cross_branch_denied_at_branch_guard_for_group_scoped_roles():
	# Branch-axis isolation: a North-scoped leader/member cannot touch a South row.
	gw = _make_gateway()
	for _, user in GROUP_SCOPED_ACTORS:
		with pytest.raises(perms.FlockPermissionError):
			perms.assert_branch_scope(doc_branch="South", user=user, gateway=gw)
		with pytest.raises(perms.FlockPermissionError):
			perms.assert_branch_scope(doc_branch="South-Campus", user=user, gateway=gw)
		# Their home branch is reachable.
		perms.assert_branch_scope(doc_branch="North", user=user, gateway=gw)


def test_cross_subtree_foreign_denied_at_group_guard_but_passes_branch_guard():
	# The cross-subtree-foreign row (North-Service, same branch North) is denied
	# by the GROUP guard (foreign subtree) even though the BRANCH guard passes
	# (same branch). This proves the two axes compose independently: same-branch
	# is not sufficient — group-axis subtree containment is enforced separately.
	gw = _make_gateway()
	for _, user in GROUP_SCOPED_ACTORS:
		# Branch guard passes (North-Service is in the North branch).
		perms.assert_branch_scope(doc_branch="North", user=user, gateway=gw)
		# Group guard denies (North-Service is foreign to every group-scoped role).
		with pytest.raises(perms.FlockPermissionError):
			perms.assert_group_scope(
				doc_group="North-Service", doc_member="M-North-Svc", user=user, gateway=gw
			)


# --------------------------------------------------------------------------- #
# Approval-authority guard (FLO-7 §4.2 / §6.2) — a non-chain leader cannot
# decide someone else's step, and a cross-branch user cannot decide even if
# identity matched. Uses approvals.StepView (the guard's duck-typed input).
# --------------------------------------------------------------------------- #
def _step_view(*, level, member, user, group, branch="North"):
	"""Build a guard-ready StepView without importing the approvals doctype layer."""
	from flock_os.approvals import StepView

	return StepView(
		idx=1,
		approver_level=level,
		approver_member=member,
		approver_user=user,
		approver_group=group,
		step_status="Pending",
		doc_branch=branch,
		doc_group=group,
	)


def test_resolved_chain_approver_in_scope_may_decide():
	# M-Lead is the resolved approver AND leads the gathering's group subtree
	# (Youth-Band sits under North-Ministries) in the same branch (North) → allow.
	gw = _make_gateway()
	step = _step_view(level=LEVEL_PARENT, member="M-Lead", user=GROUP_LEADER_USER, group="Youth-Band")
	assert perms.can_decide_approval_step(step=step, user=GROUP_LEADER_USER, gateway=gw) is True
	perms.assert_approval_scope(step=step, user=GROUP_LEADER_USER, gateway=gw)  # no raise


def test_non_chain_leader_cannot_decide_other_leaders_step():
	# M-Lead is NOT the resolved approver of M-Other's step → identity fails →
	# deny, even though M-Lead leads an ancestor subtree of Youth-Band.
	gw = _make_gateway()
	step = _step_view(level=LEVEL_PARENT, member="M-Other", user="m-other@flock", group="Youth-Band")
	assert perms.can_decide_approval_step(step=step, user=GROUP_LEADER_USER, gateway=gw) is False
	with pytest.raises(perms.FlockPermissionError):
		perms.assert_approval_scope(step=step, user=GROUP_LEADER_USER, gateway=gw)


def test_out_of_subtree_leader_cannot_decide():
	# M-Lead is the resolved approver but the gathering sits in a foreign subtree
	# (North-Service is NOT under North-Ministries) → group axis fails → deny.
	gw = _make_gateway()
	step = _step_view(level=LEVEL_PARENT, member="M-Lead", user=GROUP_LEADER_USER, group="North-Service")
	assert perms.can_decide_approval_step(step=step, user=GROUP_LEADER_USER, gateway=gw) is False


def test_cross_branch_user_cannot_decide_even_if_identity_matched():
	# Re-scope M-Lead to South: identity matches, group subtree matches, but the
	# branch axis (North gathering vs South-scoped user) fails → deny.
	gw = _make_gateway()
	gw.branch_ups_by_user[GROUP_LEADER_USER] = ("South",)
	step = _step_view(level=LEVEL_PARENT, member="M-Lead", user=GROUP_LEADER_USER, group="Youth-Band")
	assert perms.can_decide_approval_step(step=step, user=GROUP_LEADER_USER, gateway=gw) is False


def test_branch_admin_terminator_scoped_to_its_branch():
	# The terminal Branch-Admin step is a role, not a person: North's Branch Admin
	# may decide a North terminal step; a South Branch Admin may not.
	gw = _make_gateway()
	north_term = _step_view(
		level=LEVEL_BRANCH_ADMIN, member="M-BA-North", user=BRANCH_ADMIN_USER, group=None, branch="North"
	)
	assert perms.can_decide_approval_step(step=north_term, user=BRANCH_ADMIN_USER, gateway=gw) is True
	# A South admin (re-scope) cannot decide North's terminator.
	gw.branch_ups_by_user[BRANCH_ADMIN_USER] = ("South",)
	assert perms.can_decide_approval_step(step=north_term, user=BRANCH_ADMIN_USER, gateway=gw) is False


# --------------------------------------------------------------------------- #
# Dual-role broader-scope-wins (ADR §6.3 rec #6) + NULL-group passthrough.
# --------------------------------------------------------------------------- #
def test_dual_role_leader_plus_branch_admin_treated_as_branch_admin():
	# A Group Leader who is also Branch Admin is treated as Branch Admin: group
	# axis bypassed (broader scope wins), branch axis scoped to their subtree.
	gw = _make_gateway()
	gw.roles_by_user[GROUP_LEADER_USER] = frozenset({perms.ROLE_GROUP_LEADER, perms.ROLE_BRANCH_ADMIN})
	assert perms.is_bypass_user(gw.get_user_roles(GROUP_LEADER_USER)) is True
	# Group guard passes even for a foreign group now (bypass).
	perms.assert_group_scope(
		doc_group="North-Service", doc_member="M-North-Svc", user=GROUP_LEADER_USER, gateway=gw
	)
	# But the branch axis still applies (North-scoped) — cross-branch denied.
	with pytest.raises(perms.FlockPermissionError):
		perms.assert_branch_scope(doc_branch="South", user=GROUP_LEADER_USER, gateway=gw)


def test_nullable_group_doctype_passthrough_in_fragment():
	# FLO-8 §6.2: DocTypes whose `group` link is nullable (e.g. Flock
	# Announcement) emit a ``group IS NULL`` passthrough so rows with no group
	# anchor fall through to the branch axis instead of being hidden.
	scope = LeaderScope(member="M-Lead", led_bounds=NORTH_LEADER_BOUNDS, joined_groups=("Worship",))
	sql = perms.build_group_scope_sql(doctype="Flock Announcement", scope=scope, escape=_passthrough)
	assert "`tabFlock Announcement`.`group` IS NULL" in sql
	assert "`tabFlock Announcement`.`group` IN (SELECT name FROM `tabFlock Group`" in sql


# --------------------------------------------------------------------------- #
# resolve_leader_scope — the resolve step of resolve→scope→guard.
# --------------------------------------------------------------------------- #
def test_resolve_leader_scope_for_each_role_actor():
	gw = _make_gateway()
	# Group Leader resolves to the North-Ministries subtree, no joined.
	leader = perms.resolve_leader_scope(gw, user=GROUP_LEADER_USER)
	assert leader.is_leader is True
	assert {b.name for b in leader.led_bounds} == {"North-Ministries"}
	# Member resolves to no led groups, joined Worship.
	member = perms.resolve_leader_scope(gw, user=MEMBER_USER)
	assert member.is_leader is False
	assert member.joined_groups == ("Worship",)
	# Bypass roles short-circuit to an empty (no-op) scope.
	for user in (ORG_ADMIN_USER, BRANCH_ADMIN_USER, AUDITOR_USER):
		scope = perms.resolve_leader_scope(gw, user=user)
		assert scope.led_bounds == ()
		assert scope.is_leader is False


def test_guard_raises_flock_permission_error_type():
	# The deny path raises the canonical permission error (not a bare PermissionError).
	gw = _make_gateway()
	with pytest.raises(perms.FlockPermissionError) as exc:
		perms.assert_group_scope(
			doc_group="Outreach", doc_member="M-South-Lead", user=GROUP_LEADER_USER, gateway=gw
		)
	# The message names the scope anchors for auditability.
	assert "Outreach" in str(exc.value)
