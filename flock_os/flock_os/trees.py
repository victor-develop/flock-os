"""
Tree-structural operations + invariants for the two Flock OS trees
(ADR-0001 §4 / §4.4, FLO-5 §3.3/§3.4).

This module is the single chokepoint for structural tree validation **and**
traversal. Per the ADR, ``flock_os.trees`` is the *only* place the group-branch-
binding rule is enforced, and the only place pure tree traversal primitives
live (the Frappe-backed service in :mod:`flock_os.traversal` delegates here).
Structural mutations (``move_branch`` / ``move_group`` + nested-set rebuild)
land alongside these rules as the permission layer ([FLO-20](/FLO/issues/FLO-20))
needs them; the read-side traversal shipped here is the [FLO-19](/FLO/issues/FLO-19)
concern.

The pure predicates + traversal helpers below are framework-agnostic: they
operate on plain ``Mapping`` adjacency views (``parent_of`` / ``children_of``)
that the Frappe-facing service materializes once per query. This keeps the
shape of every traversal (ancestors / descendants / subtree / path / depth /
leaves / ancestry tests) unit-testable at project level (plain ``pytest``)
without a Frappe site, including deep/nested trees and defensive cycle
detection — the same DRY/testability discipline as :mod:`flock_os.flock_os.rules`.

Canonical model ([FLO-5](/FLO/issues/FLO-5) §3.4, [FLO-4](/FLO/issues/FLO-4)):

- ``Flock Branch`` (``is_tree=1``) — the administrative org tree / branch axis.
- ``Flock Group``  (``is_tree=1``) — the ministry/cell tree, **branch-bound**:
  every group subtree lives within exactly one branch. A child group's ``branch``
  must equal its parent's ``branch``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence


class FlockTreeError(ValueError):
	"""Raised when a two-tree structural invariant is violated."""


# ---------------------------------------------------------------------------- #
# Adjacency-map type aliases.
#
# A tree is described by two complementary mappings keyed on node id (the stable
# primary key — ``Flock Branch.name`` / ``Flock Group.name``):
#   * ``parent_of``   — node -> its parent id, or ``None``/missing for a root.
#   * ``children_of`` — node -> the ordered list of its direct child ids.
# Either view alone is sufficient for the primitives below; callers pass the one
# they materialized. Roots are simply nodes whose parent is ``None`` or absent.
# ---------------------------------------------------------------------------- #
ParentMap = Mapping[str, str | None]
ChildMap = Mapping[str, Sequence[str]]


def validate_group_branch_binding(*, parent_branch: str | None, child_branch: str) -> None:
	"""ADR §4.2 / FLO-5 §3.3 — a group subtree is branch-bound.

	The root group of a tree (``parent_branch is None``) sets the branch for the
	whole subtree; any child group must inherit that same branch. ``branch`` is
	therefore immutable within a group subtree.

	Raises :class:`FlockTreeError` when a child group attempts to declare a
	branch different from its parent group's branch.
	"""
	if not child_branch:
		raise FlockTreeError("Flock Group.branch is required (the group scoping anchor).")

	if parent_branch is None:
		return

	if parent_branch != child_branch:
		raise FlockTreeError(
			"A child group must inherit its parent group's branch "
			f"(parent branch {parent_branch!r} != this branch {child_branch!r}). "
			"A group subtree is branch-bound and its branch is immutable."
		)


# ---------------------------------------------------------------------------- #
# Pure traversal primitives (ADR §4.1 / §4.4).
#
# Generic over both Flock trees — they take an adjacency view and never touch a
# database. The Frappe-backed service (:mod:`flock_os.traversal`) materializes
# the view once per query and delegates here, so the shape of every traversal is
# covered by project-level tests. Cycle-safe: a malformed adjacency that loops
# back to an already-visited node raises instead of looping forever (defensive —
# Frappe nested sets cannot cycle, but the pure layer must not trust input).
# ---------------------------------------------------------------------------- #
def ancestors_of(node: str, parent_of: ParentMap) -> list[str]:
	"""Walk ``parent_of`` from ``node`` up to the root, returning the ancestor chain.

	Returns ``[parent, grandparent, ..., root]`` (excludes ``node`` itself). A
	root node yields ``[]``. Raises :class:`FlockTreeError` if the chain is
	missing or cycles.
	"""
	chain: list[str] = []
	seen: set[str] = {node}
	current = parent_of.get(node)
	while current is not None:
		if current in seen:
			raise FlockTreeError(f"Cycle detected in parent chain at {current!r} (from {node!r}).")
		chain.append(current)
		seen.add(current)
		current = parent_of.get(current)
	return chain


def path_to_root(node: str, parent_of: ParentMap) -> list[str]:
	"""``[node, parent, grandparent, ..., root]`` — inclusive of ``node``."""
	return [node, *ancestors_of(node, parent_of)]


def root_of(node: str, parent_of: ParentMap) -> str:
	"""The topmost ancestor of ``node`` (the root of its tree)."""
	chain = ancestors_of(node, parent_of)
	return chain[-1] if chain else node


def descendants_of(node: str, children_of: ChildMap) -> list[str]:
	"""All strict descendants of ``node`` (BFS, excludes ``node`` itself).

	Order is breadth-first within each level and follows the ``children_of``
	list order at each parent (stable, deterministic). Raises
	:class:`FlockTreeError` on a cycle.
	"""
	out: list[str] = []
	seen: set[str] = {node}
	frontier: list[str] = list(children_of.get(node, ()))
	while frontier:
		next_frontier: list[str] = []
		for child in frontier:
			if child in seen:
				raise FlockTreeError(f"Cycle detected in child graph at {child!r} (under {node!r}).")
			seen.add(child)
			out.append(child)
			next_frontier.extend(children_of.get(child, ()))
		frontier = next_frontier
	return out


def subtree_of(node: str, children_of: ChildMap) -> list[str]:
	"""``node`` plus all its descendants (BFS). Useful for scoped fan-out."""
	return [node, *descendants_of(node, children_of)]


def is_ancestor_of(candidate: str, node: str, parent_of: ParentMap) -> bool:
	"""True iff ``candidate`` appears in ``node``'s ancestor chain."""
	return candidate in set(ancestors_of(node, parent_of))


def is_descendant_of(candidate: str, node: str, parent_of: ParentMap) -> bool:
	"""True iff ``node`` is an ancestor of ``candidate`` (symmetric helper)."""
	return is_ancestor_of(node, candidate, parent_of)


def depth_of(node: str, parent_of: ParentMap) -> int:
	"""Distance from ``node`` to its root — ``0`` for a root, ``1`` for its child, …"""
	return len(ancestors_of(node, parent_of))


def leaves_under(node: str, children_of: ChildMap) -> list[str]:
	"""All leaf nodes in ``node``'s subtree that have no children of their own.

	A childless ``node`` yields ``[node]`` (a leaf is itself a subtree of size 1).
	"""
	subtree = subtree_of(node, children_of)
	return [n for n in subtree if not children_of.get(n)]
