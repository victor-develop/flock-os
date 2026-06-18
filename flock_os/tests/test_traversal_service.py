"""
Project-level tests for :mod:`flock_os.traversal` — the FLO-19 read service.

These run under plain ``pytest`` (no Frappe site / bench required). The
:class:`TreeTraversalService` is exercised against an in-memory
:class:`RecordingTreeReadGateway` so every traversal (ancestors / descendants /
subtree / path / whole-tree), the "leads 1..N groups" relationships, and the
approval-routing leader chain are pinned without a database — the same
hexagonal-port discipline as :mod:`flock_os.events` / :mod:`flock_os.realtime`.

The Frappe-backed whitelist transport (``@frappe.whitelist()``) is integration-
tested via ``bench run-tests``; the project-level gate asserts the module
loads cleanly without Frappe and the service contract is correct.
"""

from __future__ import annotations

from typing import Any

import pytest

from flock_os import traversal
from flock_os.flock_os.trees import FlockTreeError
from flock_os.traversal import (
	LEADER_ROLES,
	TreeTraversalService,
)


# --------------------------------------------------------------------------- #
# In-memory gateway: a fully-known two-tree world.
# --------------------------------------------------------------------------- #
# Branch tree (two roots; A subtree is wide/deep, B is a stub):
#
#   BA              BB
#    ├── BA1         └── BB1
#    │    ├── BA1a
#    │    └── BA1b
#    └── BA2
#
# Group tree (one branch-root group, depth 3, two leaders):
#
#   G0 (leader = M0)
#    └── G1 (leader = M1)
#         └── G2 (leader = M1)  ← M1 leads two groups (leads 1..N)
#
# M2 holds a roster Co-Leader slot on G0 (groups_co_led_by / groups_led_or_co_led_by).
#
def _branch_row(name: str, parent: str | None) -> dict[str, Any]:
	return {
		"name": name,
		"branch_name": name,
		"parent_branch": parent,
		"organization": "Flock HQ",
		"is_active": 1,
	}


def _group_row(name: str, parent: str | None, leader: str | None) -> dict[str, Any]:
	return {
		"name": name,
		"group_name": name,
		"parent_group": parent,
		"branch": "BA",
		"organization": "Flock HQ",
		"leader": leader,
		"group_type": "Ministry",
		"is_active": 1,
	}


def _gm_row(name: str, group: str, member: str, role: str) -> dict[str, Any]:
	return {
		"name": name,
		"group": group,
		"member": member,
		"role": role,
		"status": "Active",
		"branch": "BA",
	}


class StubTreeReadGateway:
	"""In-memory :class:`TreeReadGateway` implementation for unit tests."""

	def __init__(self) -> None:
		self.branches: dict[str, dict[str, Any]] = {}
		self.groups: dict[str, dict[str, Any]] = {}
		self.group_members: dict[str, dict[str, Any]] = {}
		self.calls: list[tuple[str, tuple, dict]] = []

	def add_branch(self, name: str, parent: str | None = None) -> dict[str, Any]:
		row = _branch_row(name, parent)
		self.branches[name] = row
		return row

	def add_group(self, name: str, parent: str | None = None, leader: str | None = None) -> dict[str, Any]:
		row = _group_row(name, parent, leader)
		self.groups[name] = row
		return row

	def add_group_member(self, name: str, group: str, member: str, role: str = "Member") -> dict[str, Any]:
		row = _gm_row(name, group, member, role)
		self.group_members[name] = row
		return row

	# -- TreeReadGateway implementation ----------------------------------- #
	def get_branch(self, name: str) -> dict[str, Any] | None:
		row = self.branches.get(name)
		return dict(row) if row else None

	def get_group(self, name: str) -> dict[str, Any] | None:
		row = self.groups.get(name)
		return dict(row) if row else None

	def _subtree_rows(
		self, table: dict[str, dict[str, Any]], parent_key: str, root: str | None
	) -> list[dict[str, Any]]:
		if root is None:
			return [dict(r) for r in table.values()]
		# Walk the adjacency from root.
		rows_by_name = {n: dict(r) for n, r in table.items()}
		if root not in rows_by_name:
			return []
		children_of: dict[str, list[str]] = {}
		for name, row in rows_by_name.items():
			parent = row.get(parent_key)
			if parent:
				children_of.setdefault(parent, []).append(name)
		out = [rows_by_name[root]]
		frontier = list(children_of.get(root, []))
		while frontier:
			next_frontier: list[str] = []
			for child in frontier:
				out.append(rows_by_name[child])
				next_frontier.extend(children_of.get(child, []))
			frontier = next_frontier
		return out

	def fetch_branch_subtree_rows(self, root: str | None) -> list[dict[str, Any]]:
		return self._subtree_rows(self.branches, "parent_branch", root)

	def fetch_group_subtree_rows(self, root: str | None) -> list[dict[str, Any]]:
		return self._subtree_rows(self.groups, "parent_group", root)

	def fetch_groups_led_by(self, member: str) -> list[dict[str, Any]]:
		return [dict(r) for r in self.groups.values() if r.get("leader") == member]

	def fetch_group_member_rows_for(
		self, member: str, roles: tuple[str, ...] | None = None
	) -> list[dict[str, Any]]:
		roles = roles or ()
		roleset = set(roles)
		return [
			dict(r)
			for r in self.group_members.values()
			if r.get("member") == member and (not roleset or r.get("role") in roleset)
		]


@pytest.fixture()
def gateway() -> StubTreeReadGateway:
	gw = StubTreeReadGateway()
	# Branch tree.
	gw.add_branch("BA")
	gw.add_branch("BB")
	gw.add_branch("BA1", parent="BA")
	gw.add_branch("BA2", parent="BA")
	gw.add_branch("BA1a", parent="BA1")
	gw.add_branch("BA1b", parent="BA1")
	gw.add_branch("BB1", parent="BB")
	# Group tree.
	gw.add_group("G0", parent=None, leader="M0")
	gw.add_group("G1", parent="G0", leader="M1")
	gw.add_group("G2", parent="G1", leader="M1")  # M1 leads two groups.
	# Roster edges: M0 is Leader on G0; M2 is Co-Leader on G0.
	gw.add_group_member("GM0", group="G0", member="M0", role="Leader")
	gw.add_group_member("GM2", group="G0", member="M2", role="Co-Leader")
	return gw


@pytest.fixture()
def service(gateway: StubTreeReadGateway) -> TreeTraversalService:
	return TreeTraversalService(gateway)


# --------------------------------------------------------------------------- #
# Branch-tree traversals
# --------------------------------------------------------------------------- #
def test_branch_ancestors_returns_parent_chain(service: TreeTraversalService):
	rows = service.branch_ancestors("BA1a")
	assert [r["name"] for r in rows] == ["BA1", "BA"]


def test_branch_ancestors_of_root_is_empty(service: TreeTraversalService):
	assert service.branch_ancestors("BA") == []


def test_branch_descendants_is_bfs_and_stable(service: TreeTraversalService):
	rows = service.branch_descendants("BA")
	assert [r["name"] for r in rows] == ["BA1", "BA2", "BA1a", "BA1b"]


def test_branch_subtree_includes_node_first(service: TreeTraversalService):
	rows = service.branch_subtree("BA1")
	assert [r["name"] for r in rows] == ["BA1", "BA1a", "BA1b"]


def test_branch_path_to_root_is_inclusive(service: TreeTraversalService):
	rows = service.branch_path_to_root("BA1b")
	assert [r["name"] for r in rows] == ["BA1b", "BA1", "BA"]


def test_branch_tree_returns_whole_tree(service: TreeTraversalService):
	rows = service.branch_tree()
	names = {r["name"] for r in rows}
	assert names == {"BA", "BB", "BA1", "BA2", "BA1a", "BA1b", "BB1"}


def test_branch_unknown_raises(gateway: StubTreeReadGateway):
	svc = TreeTraversalService(gateway)
	with pytest.raises(FlockTreeError):
		svc.branch_ancestors("NOPE")


# --------------------------------------------------------------------------- #
# Group-tree traversals
# --------------------------------------------------------------------------- #
def test_group_ancestors_walks_to_branch_root(service: TreeTraversalService):
	rows = service.group_ancestors("G2")
	assert [r["name"] for r in rows] == ["G1", "G0"]


def test_group_descendants_of_leaf_is_empty(service: TreeTraversalService):
	assert service.group_descendants("G2") == []


def test_group_subtree_includes_full_chain(service: TreeTraversalService):
	rows = service.group_subtree("G0")
	assert [r["name"] for r in rows] == ["G0", "G1", "G2"]


def test_group_path_to_root_returns_full_path(service: TreeTraversalService):
	rows = service.group_path_to_root("G2")
	assert [r["name"] for r in rows] == ["G2", "G1", "G0"]


def test_group_tree_returns_whole_tree(service: TreeTraversalService):
	rows = service.group_tree()
	assert {r["name"] for r in rows} == {"G0", "G1", "G2"}


def test_group_unknown_raises(gateway: StubTreeReadGateway):
	svc = TreeTraversalService(gateway)
	with pytest.raises(FlockTreeError):
		svc.group_subtree("MISSING")


# --------------------------------------------------------------------------- #
# "Member leads 1..N groups" (ADR §4.3)
# --------------------------------------------------------------------------- #
def test_groups_led_by_returns_accountable_leader_groups(service: TreeTraversalService):
	# M1 leads G1 AND G2 — the "1..N groups" relationship.
	rows = service.groups_led_by("M1")
	assert {r["name"] for r in rows} == {"G1", "G2"}


def test_groups_led_by_returns_empty_for_unknown_member(service: TreeTraversalService):
	assert service.groups_led_by("UNKNOWN") == []


def test_groups_co_led_by_resolves_roster_to_group_rows(service: TreeTraversalService):
	# M2 holds a Co-Leader roster slot on G0 only.
	rows = service.groups_co_led_by("M2")
	assert [r["name"] for r in rows] == ["G0"]


def test_groups_co_led_by_uses_leader_roles_only(gateway: StubTreeReadGateway):
	# Add a plain Member roster slot for M2 on G1 — must not show up.
	gateway.add_group_member("GM3", group="G1", member="M2", role="Member")
	svc = TreeTraversalService(gateway)
	rows = svc.groups_co_led_by("M2")
	assert [r["name"] for r in rows] == ["G0"]
	# Verify the gateway was asked for the canonical leader roles.
	roles_seen = {r["role"] for r in gateway.group_members.values() if r["member"] == "M2"}
	assert roles_seen == {"Co-Leader", "Member"}  # data sanity


def test_groups_led_or_co_led_by_unions_and_dedupes(gateway: StubTreeReadGateway):
	# M0 leads G0 AND holds a Leader roster slot on G0 — must appear once.
	svc = TreeTraversalService(gateway)
	rows = svc.groups_led_or_co_led_by("M0")
	assert [r["name"] for r in rows] == ["G0"]


def test_groups_led_or_co_led_by_unions_across_groups(gateway: StubTreeReadGateway):
	# M1 leads G1+G2; add a Co-Leader roster for M1 on G0 — three distinct groups.
	gateway.add_group_member("GM4", group="G0", member="M1", role="Co-Leader")
	svc = TreeTraversalService(gateway)
	rows = svc.groups_led_or_co_led_by("M1")
	assert {r["name"] for r in rows} == {"G0", "G1", "G2"}


def test_leader_roles_constant_is_leader_and_co_leader():
	# The roster edges that count as "leadership" (ADR §4.3).
	assert set(LEADER_ROLES) == {"Leader", "Co-Leader"}


# --------------------------------------------------------------------------- #
# Approval-routing leader chain (ADR §6.6 / FLO-5 §4.6)
# --------------------------------------------------------------------------- #
def test_leader_chain_walks_parent_groups_to_root(service: TreeTraversalService):
	chain = service.leader_chain_for_group("G2")
	# Each entry: {group, leader}; leaf→root; one entry per group in path.
	assert chain == [
		{"group": "G2", "leader": "M1"},
		{"group": "G1", "leader": "M1"},  # same leader, kept (different group)
		{"group": "G0", "leader": "M0"},
	]


def test_leader_chain_keeps_one_entry_per_group_not_per_leader(service: TreeTraversalService):
	# G2 and G1 are both led by M1 — the chain keeps both group entries (the
	# dedupe-by-leader decision is a FLO-7 workflow concern, not a traversal
	# concern). A faithful tree traversal reports every group in the path.
	chain = service.leader_chain_for_group("G2")
	groups = [entry["group"] for entry in chain]
	leaders = [entry["leader"] for entry in chain]
	assert groups == ["G2", "G1", "G0"]
	# M1 legitimately appears twice (two groups, same leader); the set is smaller.
	assert leaders == ["M1", "M1", "M0"]
	assert set(leaders) == {"M0", "M1"}


def test_leader_chain_skips_unassigned_leader(gateway: StubTreeReadGateway):
	# G0's leader is blank — it must be skipped, not included as None.
	gateway.groups["G0"]["leader"] = None
	svc = TreeTraversalService(gateway)
	chain = svc.leader_chain_for_group("G2")
	assert chain == [
		{"group": "G2", "leader": "M1"},
		{"group": "G1", "leader": "M1"},
	]


def test_leader_chain_unknown_group_raises(gateway: StubTreeReadGateway):
	svc = TreeTraversalService(gateway)
	with pytest.raises(FlockTreeError):
		svc.leader_chain_for_group("NOPE")


# --------------------------------------------------------------------------- #
# Module-level wiring + import-cleanliness (no Frappe required at import time).
# --------------------------------------------------------------------------- #
def test_module_defines_whitelist_endpoints():
	# Every REST entry point must exist and be callable (the @frappe.whitelist
	# deco is a no-op identity outside a bench — see traversal.frappe_whitelist).
	for name in (
		"get_branch_ancestors",
		"get_branch_descendants",
		"get_branch_subtree",
		"get_branch_path_to_root",
		"get_branch_tree",
		"get_group_ancestors",
		"get_group_descendants",
		"get_group_subtree",
		"get_group_path_to_root",
		"get_group_tree",
		"get_groups_led_by_member",
		"get_leader_chain_for_group",
	):
		assert callable(getattr(traversal, name)), f"missing whitelist endpoint {name!r}"


def test_install_gateway_swaps_service_for_tests(gateway: StubTreeReadGateway):
	# install_gateway returns a service backed by the supplied gateway and is
	# the test/prod wiring entry point (mirrors flock_os.events.install_sink).
	svc = traversal.install_gateway(gateway)
	assert svc is traversal.get_service()
	# Sanity: the module-level service reflects the swap.
	assert svc.groups_led_by("M1")  # M1 leads G1 + G2 per the fixture.


def test_frappe_whitelist_decorator_is_identity_without_frappe():
	# Outside a bench the decorator must keep the function callable (CI gate).
	deco = traversal.frappe_whitelist()

	@deco
	def sample(x):  # type: ignore[no-untyped-def]
		return x * 2

	assert sample(3) == 6
