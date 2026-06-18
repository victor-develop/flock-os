#!/usr/bin/env python3
"""
Phase-1 exit-gate demo (FLO-52) — org-tree + permission spine.

A single, reproducible demo of the Phase-1 spine shipped by [FLO-17] /
[FLO-19] / [FLO-20] and verified green by [FLO-21]. It drives the **real**
domain services — :class:`flock_os.traversal.TreeTraversalService` and the
:mod:`flock_os.permissions` scoping API — against a fully-known in-memory
multi-branch world (the same hexagonal-gateway discipline as the project test
suite), so the output is genuine evidence of the spine's behaviour, not a mock.

No Frappe bench is required: the demo composes an in-memory gateway that
implements both the ``TreeReadGateway`` and ``PermissionGateway`` ports, then
exercises the production domain code paths. Run it with::

    python scripts/demo_phase1.py        # or: ./scripts/demo-phase1.sh

Exit code is 0 only if every Phase-1 guarantee holds, so this command is itself
a gate. The four scenarios map 1:1 to the FLO-52 DoD #5 sign-off criteria:

  (a) seeds a multi-branch tree: root org -> >=2 branches (one nested) with
      nested groups + members across branches;
  (b) a scoped Branch Admin sees only their branch subtree (cross-branch rows
      are excluded at the guard and never returned by traversal);
  (c) a scoped Group Leader sees only their led group subtree (the
      ``permission_query_conditions`` hook narrows to it; foreign groups are
      denied);
  (d) an Org Admin sees the whole tree and an Auditor has read-only access
      across all branches.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Make the flock_os package importable when run directly as a script from any
# cwd (scripts/ is not on sys.path by default; an editable install is optional).
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
	sys.path.insert(0, str(_REPO_ROOT))

# Import the real spine. The flock_os package is installed editable
# (``pip install -e .``) per the README, so this works under the project venv
# without a Frappe site.
from flock_os import permissions as perms  # noqa: E402
from flock_os import traversal  # noqa: E402
from flock_os.flock_os import trees  # noqa: E402
from flock_os.permissions import (  # noqa: E402
	GLOBAL_BRANCH_ROLES,
	GroupBounds,
	PermissionGateway,
)
from flock_os.traversal import TreeReadGateway, TreeTraversalService  # noqa: E402

ORG = "Flock HQ"

# --------------------------------------------------------------------------- #
# The seeded world (DoD #5a): root org -> >=2 branches, one nested, with nested
# groups + members across branches.
# --------------------------------------------------------------------------- #
# Branch tree (the administrative / branch axis):
#
#   Flock HQ                         (root org — Org Admin / Auditor see all)
#   ├── North                        (Branch Admin root — sees North subtree)
#   │   ├── North-Campus
#   │   │   └── North-Outpost        (nested under another branch ✓)
#   │   └── North-East
#   └── South                        (independent tenant — isolation target)
#       └── South-Campus
#
# Group tree (branch-bound — every group subtree lives inside one branch):
#
#   North-Ministries (North, leader M-Lead)     lft 1  rgt 8
#   ├── Worship     (North, leader M-Lead)      lft 2  rgt 3
#   └── Youth       (North, leader M-Lead)      lft 4  rgt 7
#        └── Youth-Band (North, leader M-Other) lft 5  rgt 6
#
#   South-Ministries (South, leader M-South-Lead) lft 9  rgt 12
#   └── Outreach     (South, leader M-South-Lead) lft 10 rgt 11
#
# Nested-set bounds are precomputed (the production gateway reads them live
# from Frappe's nested set; here they are fixed data for the known world).

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

GROUP_PARENT_OF: dict[str, str | None] = {
	"North-Ministries": None,
	"Worship": "North-Ministries",
	"Youth": "North-Ministries",
	"Youth-Band": "Youth",
	"South-Ministries": None,
	"Outreach": "South-Ministries",
}
GROUP_BRANCH: dict[str, str] = {
	"North-Ministries": "North",
	"Worship": "North",
	"Youth": "North",
	"Youth-Band": "North",
	"South-Ministries": "South",
	"Outreach": "South",
}
GROUP_LEADER: dict[str, str | None] = {
	"North-Ministries": "M-Lead",
	"Worship": "M-Lead",
	"Youth": "M-Lead",
	"Youth-Band": "M-Other",
	"South-Ministries": "M-South-Lead",
	"Outreach": "M-South-Lead",
}
GROUP_BOUNDS: dict[str, GroupBounds] = {
	# One globally-unique nested set over the whole ``Flock Group`` doctype
	# (Frappe nested sets share a single lft/rgt space per tree doctype), so the
	# North and South subtrees are laid out non-overlapping. Overlapping ranges
	# would let pure-numeric containment cross branch lines — a real hazard.
	"North-Ministries": GroupBounds("North-Ministries", 1, 8),
	"Worship": GroupBounds("Worship", 2, 3),
	"Youth": GroupBounds("Youth", 4, 7),
	"Youth-Band": GroupBounds("Youth-Band", 5, 6),
	"South-Ministries": GroupBounds("South-Ministries", 9, 12),
	"Outreach": GroupBounds("Outreach", 10, 11),
}

# Member -> linked user (the Flock Member.linked_user axis, §4.3).
MEMBER_BY_USER: dict[str, str] = {
	"admin@flock": "M-Admin",
	"auditor@flock": "M-Auditor",
	"ba@north": "M-BA-North",
	"lead@north": "M-Lead",
	"lead@south": "M-South-Lead",
}


@dataclass
class DemoWorld(TreeReadGateway, PermissionGateway):
	"""In-memory world implementing both spine gateway ports (hexagonal).

	Feeds the real :class:`TreeTraversalService` and the real
	:mod:`flock_os.permissions` resolver/scopers, so the demo exercises
	production code paths end-to-end without a Frappe site.
	"""

	roles_by_user: dict[str, frozenset[str]] = field(default_factory=dict)

	# -- TreeReadGateway ---------------------------------------------------- #
	def get_branch(self, name: str) -> dict[str, Any] | None:
		if name not in BRANCH_PARENT_OF:
			return None
		return {"name": name, "branch_name": name, "parent_branch": BRANCH_PARENT_OF[name]}

	def get_group(self, name: str) -> dict[str, Any] | None:
		if name not in GROUP_PARENT_OF:
			return None
		return {
			"name": name,
			"group_name": name,
			"parent_group": GROUP_PARENT_OF[name],
			"branch": GROUP_BRANCH[name],
			"leader": GROUP_LEADER[name],
		}

	def _subtree(self, parent_of: dict[str, str | None], root: str | None) -> list[str]:
		if root is None:
			return list(parent_of)
		if root not in parent_of:
			return []
		children_of: dict[str, list[str]] = {}
		for name, parent in parent_of.items():
			if parent:
				children_of.setdefault(parent, []).append(name)
		out = [root]
		frontier = list(children_of.get(root, []))
		while frontier:
			nxt: list[str] = []
			for child in frontier:
				out.append(child)
				nxt.extend(children_of.get(child, []))
			frontier = nxt
		return out

	def fetch_branch_subtree_rows(self, root: str | None) -> list[dict[str, Any]]:
		return [self.get_branch(n) for n in self._subtree(BRANCH_PARENT_OF, root)]  # type: ignore[misc]

	def fetch_group_subtree_rows(self, root: str | None) -> list[dict[str, Any]]:
		return [self.get_group(n) for n in self._subtree(GROUP_PARENT_OF, root)]  # type: ignore[misc]

	def fetch_groups_led_by(self, member: str) -> list[dict[str, Any]]:
		return [self.get_group(n) for n, ld in GROUP_LEADER.items() if ld == member]  # type: ignore[misc]

	def fetch_group_member_rows_for(self, member: str, roles: Sequence[str]) -> list[dict[str, Any]]:  # noqa: ARG002
		# Leadership roster edges (ADR §4.3). The demo's led scope is expressed
		# via Flock Group.leader; roster edges would resolve here identically.
		return []

	# -- PermissionGateway -------------------------------------------------- #
	def get_user_roles(self, user: str) -> frozenset[str]:
		return self.roles_by_user.get(user, frozenset())

	def resolve_member_for_user(self, user: str) -> str | None:
		return MEMBER_BY_USER.get(user)

	def fetch_led_group_bounds(self, member: str) -> tuple[GroupBounds, ...]:
		return tuple(GROUP_BOUNDS[name] for name, ld in GROUP_LEADER.items() if ld == member)

	def fetch_joined_group_names(self, member: str) -> tuple[str, ...]:  # noqa: ARG002
		return ()

	def fetch_group_bounds(self, name: str) -> GroupBounds | None:
		return GROUP_BOUNDS.get(name)

	def list_branch_user_permissions(self, user: str) -> tuple[str, ...]:
		# Native branch axis (§6.2): a Branch Admin's materialized subtree.
		roles = self.roles_by_user.get(user, frozenset())
		if roles & GLOBAL_BRANCH_ROLES:
			return ()  # Org Admin / Auditor carry no branch UP.
		if perms.ROLE_BRANCH_ADMIN in roles:
			# Materialize this admin's branch subtree (equality rows Frappe filters on).
			home = "North" if user == "ba@north" else "South"
			return perms.compute_branch_subtree(
				home, parent_of=BRANCH_PARENT_OF, children_of=BRANCH_CHILDREN_OF
			)
		return ()


def _seed_world() -> DemoWorld:
	"""Build the seeded multi-branch world (DoD #5a)."""
	return DemoWorld(
		roles_by_user={
			"admin@flock": frozenset({perms.ROLE_ORG_ADMIN}),
			"auditor@flock": frozenset({perms.ROLE_AUDITOR}),
			"ba@north": frozenset({perms.ROLE_BRANCH_ADMIN}),
			"lead@north": frozenset({perms.ROLE_GROUP_LEADER}),
			"lead@south": frozenset({perms.ROLE_GROUP_LEADER}),
		}
	)


def _names(rows: list[dict[str, Any]]) -> list[str]:
	return [r["name"] for r in rows]


@dataclass
class Result:
	label: str
	ok: bool
	detail: str


def _scenario_a_seed(world: DemoWorld) -> Result:
	"""DoD #5a — the seeded tree shape is a real multi-branch org/group world."""
	branches = len(ALL_BRANCHES)
	orgs = sum(1 for b in ALL_BRANCHES if BRANCH_PARENT_OF[b] is None)
	nested = "North-Outpost" in BRANCH_PARENT_OF.values() or any(
		(BRANCH_PARENT_OF[B] or "") == "North-Campus" for B in ALL_BRANCHES
	)
	groups = len(GROUP_PARENT_OF)
	cross_branch = {"North", "South"} <= {GROUP_BRANCH[g] for g in GROUP_PARENT_OF}
	# Sanity: the spine's structural rule rejects a group whose branch differs
	# from its parent group's branch (branch-bound invariant, ADR §4.2).
	binding_ok = True
	for g, parent in GROUP_PARENT_OF.items():
		if parent is not None:
			try:
				trees.validate_group_branch_binding(
					parent_branch=GROUP_BRANCH[parent], child_branch=GROUP_BRANCH[g]
				)
			except trees.FlockTreeError:
				binding_ok = False
	ok = branches >= 3 and orgs >= 1 and nested and groups >= 4 and cross_branch and binding_ok
	detail = (
		f"{branches} branches ({orgs} root org), nested branch present={nested}, "
		f"{groups} groups across branches={cross_branch}, branch-bound invariant={binding_ok}"
	)
	return Result("a) seed multi-branch tree", ok, detail)


def _scenario_b_branch_admin(world: DemoWorld) -> Result:
	"""DoD #5b — Branch Admin sees only their branch subtree; cross-branch excluded."""
	svc = TreeTraversalService(world)
	# Traversal returns ONLY the North subtree (no South leakage).
	north_subtree = _names(svc.branch_subtree("North"))
	leaks_south = any(b in north_subtree for b in ("South", "South-Campus"))
	includes_nested_descendant = "North-Outpost" in north_subtree  # nested under North-Campus
	# The guard is the single chokepoint every endpoint asserts (§6.5):
	#   touching a South row from a North admin MUST raise.
	cross_blocked = False
	try:
		perms.assert_branch_scope(doc_branch="South", user="ba@north", gateway=world)
	except perms.FlockPermissionError:
		cross_blocked = True
	# And every North-descendant row is reachable (scope grows down, not up).
	own_ok = True
	try:
		for b in north_subtree:
			perms.assert_branch_scope(doc_branch=b, user="ba@north", gateway=world)
	except perms.FlockPermissionError:
		own_ok = False
	ok = (not leaks_south) and cross_blocked and own_ok and includes_nested_descendant
	detail = (
		f"branch_subtree('North')={north_subtree}; leaks South={leaks_south}; "
		f"cross-branch denied at guard={cross_blocked}; own subtree reachable={own_ok}"
	)
	return Result("b) Branch Admin scoped to North subtree", ok, detail)


def _scenario_c_group_leader(world: DemoWorld) -> Result:
	"""DoD #5c — Group Leader sees only their led group subtree; foreign denied."""
	svc = TreeTraversalService(world)
	# Traversal: the leader's led group subtree (only North-Ministries branch).
	led_subtree = _names(svc.group_subtree("North-Ministries"))
	leaks_south = any(g in led_subtree for g in ("South-Ministries", "Outreach"))
	# Permission hook narrows the group axis to the led subtree (non-empty fragment).
	perms.install_gateway(world)
	try:
		fragment = perms.get_group_scoped_conditions("Flock Group", "lead@north")
		has_scope = perms.has_group_scope("Flock Group", "lead@north")
	finally:
		perms.install_gateway(perms.NullPermissionGateway())
	fragment_narrows = bool(fragment.strip()) and has_scope
	# A foreign group (outside the led subtree) is denied at the guard.
	foreign_denied = False
	try:
		perms.assert_group_scope(
			doc_group="South-Ministries",
			doc_member="M-South-Lead",
			user="lead@north",
			gateway=world,
		)
	except perms.FlockPermissionError:
		foreign_denied = True
	# But a descendant group inside the led subtree is allowed (subtree containment, §6.3).
	descendant_ok = True
	try:
		perms.assert_group_scope(
			doc_group="Youth-Band",
			doc_member="M-Other",
			user="lead@north",
			gateway=world,
		)
	except perms.FlockPermissionError:
		descendant_ok = False
	ok = (not leaks_south) and fragment_narrows and foreign_denied and descendant_ok
	detail = (
		f"group_subtree('North-Ministries')={led_subtree}; leaks South={leaks_south}; "
		f"group-axis fragment emitted={fragment_narrows}; foreign group denied={foreign_denied}; "
		f"descendant allowed={descendant_ok}"
	)
	return Result("c) Group Leader scoped to led subtree", ok, detail)


def _scenario_d_org_admin_and_auditor(world: DemoWorld) -> Result:
	"""DoD #5d — Org Admin sees the whole tree; Auditor is read-only across all."""
	svc = TreeTraversalService(world)
	# Org Admin sees the whole branch tree (traversal) and bypasses scope.
	whole_tree = set(_names(svc.branch_tree()))
	sees_all = whole_tree == set(ALL_BRANCHES)
	org_bypass = perms.is_bypass_user([perms.ROLE_ORG_ADMIN])
	# Auditor: bypass user, no branch restriction (sees every branch at the guard),
	# and carries the read-only role (write-blocking is DocPerm's job, §4.1).
	auditor_bypass = perms.is_bypass_user([perms.ROLE_AUDITOR])
	auditor_global = perms.ROLE_AUDITOR in GLOBAL_BRANCH_ROLES
	auditor_branches_visible: list[str] = []
	for b in ALL_BRANCHES:
		try:
			perms.assert_branch_scope(doc_branch=b, user="auditor@flock", gateway=world)
			auditor_branches_visible.append(b)
		except perms.FlockPermissionError:
			pass
	auditor_sees_all = len(auditor_branches_visible) == len(ALL_BRANCHES)
	# Org admin also has no group-axis fragment (bypass).
	perms.install_gateway(world)
	try:
		org_no_fragment = perms.get_group_scoped_conditions("Flock Group", "admin@flock") == ""
		auditor_no_branch_up = world.list_branch_user_permissions("auditor@flock") == ()
	finally:
		perms.install_gateway(perms.NullPermissionGateway())
	ok = (
		sees_all
		and org_bypass
		and org_no_fragment
		and auditor_bypass
		and auditor_global
		and auditor_sees_all
		and auditor_no_branch_up
	)
	detail = (
		f"Org Admin branch_tree()={len(whole_tree)} branches (all={sees_all}), "
		f"bypass={org_bypass}, no group fragment={org_no_fragment}; "
		f"Auditor bypass={auditor_bypass}, global-branch role={auditor_global}, "
		f"sees {len(auditor_branches_visible)}/{len(ALL_BRANCHES)} branches, "
		f"no branch UP={auditor_no_branch_up} (read-only enforced via DocPerm)"
	)
	return Result("d) Org Admin full tree + Auditor read-only all", ok, detail)


SCENARIOS = (
	_scenario_a_seed,
	_scenario_b_branch_admin,
	_scenario_c_group_leader,
	_scenario_d_org_admin_and_auditor,
)


def _print_tree() -> None:
	print("Org / branch tree:")
	print("  Flock HQ")
	print("  ├── North                       <- Branch Admin scope root")
	print("  │   ├── North-Campus")
	print("  │   │   └── North-Outpost       <- nested under another branch")
	print("  │   └── North-East")
	print("  └── South                       <- independent tenant (isolation target)")
	print("      └── South-Campus")
	print("Group tree (branch-bound):")
	print("  North-Ministries (leader M-Lead)   [North]")
	print("  ├── Worship (leader M-Lead)        [North]")
	print("  └── Youth (leader M-Lead)          [North]")
	print("       └── Youth-Band (leader M-Other) [North]")
	print("  South-Ministries (leader M-South-Lead) [South]")
	print("  └── Outreach (leader M-South-Lead)     [South]")


def run() -> list[Result]:
	"""Run all Phase-1 demo scenarios against the real spine. Returns results."""
	world = _seed_world()
	traversal.install_gateway(world)  # not required for direct svc use; mirrors prod wiring
	print("=" * 72)
	print("Flock OS — Phase-1 Exit Gate demo (FLO-52): org-tree + permission spine")
	print("=" * 72)
	_print_tree()
	print()
	results: list[Result] = []
	for scenario in SCENARIOS:
		res = scenario(world)
		results.append(res)
		mark = "PASS" if res.ok else "FAIL"
		print(f"[{mark}] {res.label}")
		print(f"       {res.detail}")
	print("-" * 72)
	all_ok = all(r.ok for r in results)
	green = sum(r.ok for r in results)
	print(f"DEMO: {'PASS' if all_ok else 'FAIL'} — {green}/{len(results)} scenarios green")
	print(
		"Drives real flock_os.traversal.TreeTraversalService + flock_os.permissions "
		"(resolve_leader_scope / assert_branch_scope / assert_group_scope / "
		"get_group_scoped_conditions) over an in-memory gateway — no Frappe site."
	)
	return results


def main() -> int:
	results = run()
	return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
	sys.exit(main())
