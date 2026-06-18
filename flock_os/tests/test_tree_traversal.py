"""
Project-level tests for the pure tree-traversal primitives (FLO-19).

These run under plain ``pytest`` (no Frappe site / bench required) and pin the
shape of every traversal added to :mod:`flock_os.flock_os.trees` — the pure
adjacency-view helpers that the Frappe-backed service
(:mod:`flock_os.traversal`) delegates to. Coverage spans deep/nested trees,
wide trees, single-node trees, cycle defense, and the existing
group-branch-binding invariant shipped by [FLO-17](/FLO/issues/FLO-17).

Canonical model: ADR-0001 §4.1/§4.4 + FLO-5 §3.4 — exactly two native Frappe
trees (``Flock Branch`` admin axis, ``Flock Group`` ministry axis), both with
``parent_*`` adjacency the service materializes from the nested set.
"""

from __future__ import annotations

import pytest

from flock_os.flock_os import trees

# --------------------------------------------------------------------------- #
# Adjacency fixtures — small, fully-known shapes for assertions.
# --------------------------------------------------------------------------- #
# Branch tree (deep + wide, two roots):
#
#   RootA           RootB
#    ├── R1          └── B1
#    │    ├── R1a
#    │    └── R1b
#    └── R2
#         └── R2a
#
BRANCH_PARENT_OF: dict[str, str | None] = {
	"RootA": None,
	"RootB": None,
	"R1": "RootA",
	"R2": "RootA",
	"R1a": "R1",
	"R1b": "R1",
	"R2a": "R2",
	"B1": "RootB",
}
BRANCH_CHILDREN_OF: dict[str, list[str]] = {
	"RootA": ["R1", "R2"],
	"RootB": ["B1"],
	"R1": ["R1a", "R1b"],
	"R2": ["R2a"],
	"R1a": [],
	"R1b": [],
	"R2a": [],
	"B1": [],
}

# Group tree (single root, depth 4 — covers "deep" traversal):
#
#   G0 (branch root)
#    └── G1
#         └── G2
#              ├── G2a
#              └── G2b
#
GROUP_PARENT_OF: dict[str, str | None] = {
	"G0": None,
	"G1": "G0",
	"G2": "G1",
	"G2a": "G2",
	"G2b": "G2",
}
GROUP_CHILDREN_OF: dict[str, list[str]] = {
	"G0": ["G1"],
	"G1": ["G2"],
	"G2": ["G2a", "G2b"],
	"G2a": [],
	"G2b": [],
}


# --------------------------------------------------------------------------- #
# ancestors_of / path_to_root / root_of
# --------------------------------------------------------------------------- #
def test_ancestors_walks_to_root_excluding_self():
	assert trees.ancestors_of("R1a", BRANCH_PARENT_OF) == ["R1", "RootA"]
	assert trees.ancestors_of("B1", BRANCH_PARENT_OF) == ["RootB"]
	assert trees.ancestors_of("RootA", BRANCH_PARENT_OF) == []


def test_path_to_root_is_inclusive_of_node():
	assert trees.path_to_root("R2a", BRANCH_PARENT_OF) == ["R2a", "R2", "RootA"]
	assert trees.path_to_root("RootB", BRANCH_PARENT_OF) == ["RootB"]


def test_root_of_returns_topmost_ancestor():
	assert trees.root_of("R1b", BRANCH_PARENT_OF) == "RootA"
	assert trees.root_of("B1", BRANCH_PARENT_OF) == "RootB"
	assert trees.root_of("RootA", BRANCH_PARENT_OF) == "RootA"


def test_ancestors_handles_deep_group_chain():
	# Depth-4 group tree — exercises repeated parent walks.
	assert trees.ancestors_of("G2b", GROUP_PARENT_OF) == ["G2", "G1", "G0"]
	assert trees.depth_of("G2b", GROUP_PARENT_OF) == 3
	assert trees.depth_of("G0", GROUP_PARENT_OF) == 0


def test_ancestors_detects_cycle_in_parent_chain():
	cycle = {"X": "Y", "Y": "X"}
	with pytest.raises(trees.FlockTreeError):
		trees.ancestors_of("X", cycle)


# --------------------------------------------------------------------------- #
# descendants_of / subtree_of / leaves_under
# --------------------------------------------------------------------------- #
def test_descendants_bfs_excludes_node_and_is_stable():
	# BFS order: [R1, R2, R1a, R1b, R2a] — level by level, sibling order preserved.
	assert trees.descendants_of("RootA", BRANCH_CHILDREN_OF) == ["R1", "R2", "R1a", "R1b", "R2a"]


def test_descendants_of_leaf_is_empty():
	assert trees.descendants_of("R1a", BRANCH_CHILDREN_OF) == []
	assert trees.descendants_of("B1", BRANCH_CHILDREN_OF) == []


def test_subtree_includes_node_first():
	assert trees.subtree_of("R1", BRANCH_CHILDREN_OF) == ["R1", "R1a", "R1b"]
	assert trees.subtree_of("RootB", BRANCH_CHILDREN_OF) == ["RootB", "B1"]


def test_descendants_detects_cycle_in_child_graph():
	cycle = {"X": ["Y"], "Y": ["X"]}
	with pytest.raises(trees.FlockTreeError):
		trees.descendants_of("X", cycle)


def test_leaves_under_returns_only_leaf_nodes():
	# RootA's subtree: leaves are R1a, R1b, R2a (R1/R2 have children; RootA is root).
	assert trees.leaves_under("RootA", BRANCH_CHILDREN_OF) == ["R1a", "R1b", "R2a"]


def test_leaves_under_node_with_no_children_returns_node():
	# A leaf is a subtree of size 1.
	assert trees.leaves_under("R1a", BRANCH_CHILDREN_OF) == ["R1a"]


# --------------------------------------------------------------------------- #
# is_ancestor_of / is_descendant_of / depth_of
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
	"candidate,node,expected",
	[
		("RootA", "R1a", True),
		("R1", "R1a", True),
		("RootA", "RootA", False),  # not a strict ancestor of itself
		("R2", "R1a", False),  # sibling subtree
		("RootB", "R1a", False),  # different root
	],
)
def test_is_ancestor_of(candidate, node, expected):
	assert trees.is_ancestor_of(candidate, node, BRANCH_PARENT_OF) is expected


def test_is_descendant_of_is_symmetric():
	assert trees.is_descendant_of("R1a", "RootA", BRANCH_PARENT_OF) is True
	assert trees.is_descendant_of("R2a", "R1", BRANCH_PARENT_OF) is False


def test_depth_of_counts_hops_to_root():
	assert trees.depth_of("RootA", BRANCH_PARENT_OF) == 0
	assert trees.depth_of("R1", BRANCH_PARENT_OF) == 1
	assert trees.depth_of("R1a", BRANCH_PARENT_OF) == 2


# --------------------------------------------------------------------------- #
# Single-node tree edge case (root with no children).
# --------------------------------------------------------------------------- #
def test_single_node_tree_traversals():
	parent_of = {"Solo": None}
	children_of: dict[str, list[str]] = {"Solo": []}
	assert trees.ancestors_of("Solo", parent_of) == []
	assert trees.descendants_of("Solo", children_of) == []
	assert trees.subtree_of("Solo", children_of) == ["Solo"]
	assert trees.root_of("Solo", parent_of) == "Solo"
	assert trees.depth_of("Solo", parent_of) == 0
	assert trees.leaves_under("Solo", children_of) == ["Solo"]
