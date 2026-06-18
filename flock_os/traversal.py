"""
Org-tree / group-tree traversal service + REST read endpoints (FLO-19).

The read layer over the two Flock OS trees built in [FLO-17](/FLO/issues/FLO-17):
exposes ancestors / descendants / subtree / path-to-root lookups at any node,
the "member leads 1..N groups" relationships (ADR-0001 §4.3), and the leader
chain up a group subtree (ADR-0001 §6.6 / FLO-5 §4.6 — the basis for
[FLO-7](/FLO/issues/FLO-7) approval routing).

Layering (ADR-0001 §2 separation of concerns; AGENTS.md)::

    REST  @frappe.whitelist()  (this module, transport)
      -> TreeTraversalService  (this module, domain service)
            |-> flock_os.flock_os.trees primitives  (pure, no I/O)
            |-> TreeReadGateway port                (DB fetch, hexagonal)

The :class:`TreeReadGateway` port wraps the only Frappe calls (``get_all`` over
the native nested sets, ``get_value`` for parent links). Production:
:class:`FrappeTreeReadGateway` (lazy Frappe import). Unit tests:
:class:`RecordingTreeReadGateway`. This keeps :class:`TreeTraversalService`
fully unit-testable without a bench — the *shape* of every traversal is covered
at project level (deep / nested / wide trees), exactly like the existing
service modules (:mod:`flock_os.reporting`, :mod:`flock_os.realtime`).

Permission posture: every read goes through ``frappe.get_all`` / ``get_value``,
which applies the caller's role + User Permissions automatically (ADR §6.2 —
the branch axis rides native Frappe scoping). The custom group-tree
``permission_query_conditions`` hook ([FLO-20](/FLO/issues/FLO-20)) narrows the
group axis once it lands; until then this module returns the structurally
correct answer and relies on the native layer for tenant isolation.

REST surface (FLO-19 DoD #2):

    GET  /api/method/flock_os.traversal.get_branch_subtree?branch=<name>
    GET  /api/method/flock_os.traversal.get_branch_ancestors?branch=<name>
    GET  /api/method/flock_os.traversal.get_branch_descendants?branch=<name>
    GET  /api/method/flock_os.traversal.get_group_subtree?group=<name>
    GET  /api/method/flock_os.traversal.get_group_ancestors?group=<name>
    GET  /api/method/flock_os.traversal.get_group_descendants?group=<name>
    GET  /api/method/flock_os.traversal.get_groups_led_by_member?member=<name>
    GET  /api/method/flock_os.traversal.get_leader_chain_for_group?group=<name>
    GET  /api/method/flock_os.traversal.get_branch_tree              (whole tree)
    GET  /api/method/flock_os.traversal.get_group_tree               (whole tree)
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any, Protocol, runtime_checkable

from flock_os.flock_os import trees
from flock_os.flock_os.trees import (
	ChildMap,
	FlockTreeError,
	ParentMap,
)

# ---------------------------------------------------------------------------- #
# Row-shape contracts: the minimal field set each tree row exposes. Keeping the
# field list explicit (rather than ``*``) makes the REST contract stable and
# lets the recording gateway build rows that exercise the service fully.
# ---------------------------------------------------------------------------- #
BRANCH_ROW_FIELDS: tuple[str, ...] = (
	"name",
	"branch_name",
	"parent_branch",
	"organization",
	"is_active",
)
"""Fields returned for every Flock Branch row in a traversal result."""

GROUP_ROW_FIELDS: tuple[str, ...] = (
	"name",
	"group_name",
	"parent_group",
	"branch",
	"organization",
	"leader",
	"group_type",
	"is_active",
)
"""Fields returned for every Flock Group row in a traversal result."""

GROUP_MEMBER_ROW_FIELDS: tuple[str, ...] = (
	"name",
	"group",
	"member",
	"role",
	"status",
	"branch",
)
"""Fields returned for every Flock Group Member row in a leader-roster query."""

# The leadership roster roles (ADR-0001 §4.3). ``Leader`` = single accountable
# leader on Flock Group.leader; ``Co-Leader`` = the roster edge. Together they
# form the "leads 1..N groups" relationship surfaced by this service.
LEADER_ROLES: tuple[str, ...] = ("Leader", "Co-Leader")


# ---------------------------------------------------------------------------- #
# Gateway port (hexagonal) — the only Frappe-touching surface in this module.
# ---------------------------------------------------------------------------- #
@runtime_checkable
class TreeReadGateway(Protocol):
	"""Port: DB-read primitives :class:`TreeTraversalService` needs.

	Production adapter: :class:`FrappeTreeReadGateway` (lazy Frappe import). Unit
	tests: :class:`RecordingTreeReadGateway`. Each fetch returns plain dicts so
	the service stays Frappe-agnostic and transport-agnostic.
	"""

	def get_branch(self, name: str) -> dict[str, Any] | None:
		"""One Flock Branch row (``BRANCH_ROW_FIELDS``) or ``None`` if absent."""
		...

	def get_group(self, name: str) -> dict[str, Any] | None:
		"""One Flock Group row (``GROUP_ROW_FIELDS``) or ``None`` if absent."""
		...

	def fetch_branch_subtree_rows(self, root: str | None) -> list[dict[str, Any]]:
		"""Flock Branch rows for ``root`` + its descendants; whole tree if ``root`` is None.

		Index-backed by the native nested set (``lft``/``rgt`` range, ADR §6) so a
		deep traversal is O(depth)-cheap. Caller-permission-filtered by Frappe.
		"""
		...

	def fetch_group_subtree_rows(self, root: str | None) -> list[dict[str, Any]]:
		"""Flock Group rows for ``root`` + its descendants; whole tree if ``root`` is None."""
		...

	def fetch_groups_led_by(self, member: str) -> list[dict[str, Any]]:
		"""Flock Group rows where ``leader == member`` (the single-accountable axis)."""
		...

	def fetch_group_member_rows_for(self, member: str, roles: Sequence[str]) -> list[dict[str, Any]]:
		"""Flock Group Member rows where ``member == X`` and ``role`` ∈ ``roles``."""
		...


class NullTreeReadGateway:
	"""Empty gateway — the default before production wiring; yields no rows."""

	def get_branch(self, name: str) -> dict[str, Any] | None:  # noqa: ARG002
		return None

	def get_group(self, name: str) -> dict[str, Any] | None:  # noqa: ARG002
		return None

	def fetch_branch_subtree_rows(self, root: str | None) -> list[dict[str, Any]]:  # noqa: ARG002
		return []

	def fetch_group_subtree_rows(self, root: str | None) -> list[dict[str, Any]]:  # noqa: ARG002
		return []

	def fetch_groups_led_by(self, member: str) -> list[dict[str, Any]]:  # noqa: ARG002
		return []

	def fetch_group_member_rows_for(  # noqa: ARG002
		self, member: str, roles: Sequence[str]
	) -> list[dict[str, Any]]:
		return []


class FrappeTreeReadGateway:
	"""Production adapter — wraps ``frappe.get_all`` / ``frappe.db.get_value``.

	Lazily imports Frappe so this module stays import-clean in CI (no bench).
	All reads go through ``frappe.get_all`` so the caller's role + User
	Permissions apply automatically (ADR §6.2). The custom group-tree hook
	([FLO-20](/FLO/issues/FLO-20)) further narrows the group axis once wired.
	"""

	@property
	def _frappe(self):  # type: ignore[no-untyped-def]
		import frappe

		return frappe

	def get_branch(self, name: str) -> dict[str, Any] | None:
		frappe = self._frappe
		if not frappe.db.exists("Flock Branch", name):
			return None
		return frappe.db.get_value("Flock Branch", name, BRANCH_ROW_FIELDS, as_dict=True)

	def get_group(self, name: str) -> dict[str, Any] | None:
		frappe = self._frappe
		if not frappe.db.exists("Flock Group", name):
			return None
		return frappe.db.get_value("Flock Group", name, GROUP_ROW_FIELDS, as_dict=True)

	def fetch_branch_subtree_rows(self, root: str | None) -> list[dict[str, Any]]:
		frappe = self._frappe
		if root is None:
			return frappe.get_all("Flock Branch", fields=list(BRANCH_ROW_FIELDS))
		bounds = frappe.db.get_value("Flock Branch", root, ["lft", "rgt"])
		if not bounds:
			return []
		lft, rgt = bounds
		return frappe.get_all(
			"Flock Branch",
			filters={"lft": [">=", lft], "rgt": ["<=", rgt]},
			fields=list(BRANCH_ROW_FIELDS),
			order_by="lft asc",
		)

	def fetch_group_subtree_rows(self, root: str | None) -> list[dict[str, Any]]:
		frappe = self._frappe
		if root is None:
			return frappe.get_all("Flock Group", fields=list(GROUP_ROW_FIELDS))
		bounds = frappe.db.get_value("Flock Group", root, ["lft", "rgt"])
		if not bounds:
			return []
		lft, rgt = bounds
		return frappe.get_all(
			"Flock Group",
			filters={"lft": [">=", lft], "rgt": ["<=", rgt]},
			fields=list(GROUP_ROW_FIELDS),
			order_by="lft asc",
		)

	def fetch_groups_led_by(self, member: str) -> list[dict[str, Any]]:
		frappe = self._frappe
		return frappe.get_all(
			"Flock Group",
			filters={"leader": member},
			fields=list(GROUP_ROW_FIELDS),
			order_by="group_name asc",
		)

	def fetch_group_member_rows_for(self, member: str, roles: Sequence[str]) -> list[dict[str, Any]]:
		frappe = self._frappe
		filters = {"member": member}
		if roles:
			filters["role"] = ["in", list(roles)]
		return frappe.get_all(
			"Flock Group Member",
			filters=filters,
			fields=list(GROUP_MEMBER_ROW_FIELDS),
			order_by="group asc",
		)


# ---------------------------------------------------------------------------- #
# Domain service — composes the gateway fetches with the pure primitives.
# ---------------------------------------------------------------------------- #
class TreeTraversalService:
	"""Org-tree / group-tree traversal service (ADR-0001 §4.4, FLO-19).

	The service materializes an adjacency view (``parent_of`` / ``children_of``)
	from the gateway rows **once per query** and delegates the actual traversal
	to :mod:`flock_os.flock_os.trees` — keeping the shape of every traversal
	unit-tested without a database. It always returns the **row payloads** (not
	bare ids) so the REST surface is stable and the frontend can render without
	a follow-up fetch.

	Results are ordered root→leaf (BFS for descendants, leaf→root for ancestors)
	and stable across calls. Unknown nodes raise :class:`FlockTreeError`.
	"""

	def __init__(self, gateway: TreeReadGateway) -> None:
		self._gw = gateway

	# -- helpers ------------------------------------------------------------- #
	@staticmethod
	def _build_adjacency(
		rows: Sequence[dict[str, Any]], parent_key: str
	) -> tuple[ParentMap, ChildMap, dict[str, dict[str, Any]]]:
		"""Materialize ``(parent_of, children_of, row_by_name)`` from a row set.

		``parent_key`` is ``"parent_branch"`` for the branch tree and
		``"parent_group"`` for the group tree. Children keep their stored order
		(Frappe returns rows ordered by ``lft`` asc, so children_of is BFS-stable).
		"""
		parent_of: dict[str, str | None] = {}
		children_of: dict[str, list[str]] = defaultdict(list)
		row_by_name: dict[str, dict[str, Any]] = {}
		for row in rows:
			name = row["name"]
			parent = row.get(parent_key) or None
			parent_of[name] = parent
			row_by_name[name] = dict(row)
			if parent is not None:
				children_of[parent].append(name)
		return parent_of, dict(children_of), row_by_name

	@staticmethod
	def _rows_for(names: Sequence[str], row_by_name: Mapping[str, dict[str, Any]]) -> list[dict[str, Any]]:
		"""Project a name list back to its rows, skipping any unknown (defensive)."""
		return [dict(row_by_name[n]) for n in names if n in row_by_name]

	def _branch_subtree_view(self, root: str | None) -> tuple[ParentMap, ChildMap, dict[str, dict[str, Any]]]:
		rows = self._gw.fetch_branch_subtree_rows(root)
		return self._build_adjacency(rows, "parent_branch")

	def _group_subtree_view(self, root: str | None) -> tuple[ParentMap, ChildMap, dict[str, dict[str, Any]]]:
		rows = self._gw.fetch_group_subtree_rows(root)
		return self._build_adjacency(rows, "parent_group")

	def _require_branch(self, name: str) -> dict[str, Any]:
		row = self._gw.get_branch(name)
		if row is None:
			raise FlockTreeError(f"Flock Branch {name!r} not found.")
		return dict(row)

	def _require_group(self, name: str) -> dict[str, Any]:
		row = self._gw.get_group(name)
		if row is None:
			raise FlockTreeError(f"Flock Group {name!r} not found.")
		return dict(row)

	# -- branch tree --------------------------------------------------------- #
	def branch_ancestors(self, branch: str) -> list[dict[str, Any]]:
		"""Rows from ``branch``'s parent up to the root (excludes ``branch``)."""
		self._require_branch(branch)
		parent_of, _, row_by_name = self._branch_subtree_view(None)
		return self._rows_for(trees.ancestors_of(branch, parent_of), row_by_name)

	def branch_descendants(self, branch: str) -> list[dict[str, Any]]:
		"""All branch rows strictly under ``branch`` (BFS, excludes ``branch``)."""
		self._require_branch(branch)
		_, children_of, row_by_name = self._branch_subtree_view(branch)
		return self._rows_for(trees.descendants_of(branch, children_of), row_by_name)

	def branch_subtree(self, branch: str) -> list[dict[str, Any]]:
		"""``branch`` plus all its descendants (the full scoped subtree)."""
		self._require_branch(branch)
		_, children_of, row_by_name = self._branch_subtree_view(branch)
		return self._rows_for(trees.subtree_of(branch, children_of), row_by_name)

	def branch_path_to_root(self, branch: str) -> list[dict[str, Any]]:
		"""``[branch, parent, ..., root]`` — inclusive path for breadcrumbs."""
		self._require_branch(branch)
		parent_of, _, row_by_name = self._branch_subtree_view(None)
		return self._rows_for(trees.path_to_root(branch, parent_of), row_by_name)

	def branch_tree(self) -> list[dict[str, Any]]:
		"""Every visible Flock Branch row (whole tree). Permission-filtered by Frappe."""
		return [dict(r) for r in self._gw.fetch_branch_subtree_rows(None)]

	# -- group tree ---------------------------------------------------------- #
	def group_ancestors(self, group: str) -> list[dict[str, Any]]:
		"""Rows from ``group``'s parent up to the branch-root group (excludes ``group``)."""
		self._require_group(group)
		parent_of, _, row_by_name = self._group_subtree_view(None)
		return self._rows_for(trees.ancestors_of(group, parent_of), row_by_name)

	def group_descendants(self, group: str) -> list[dict[str, Any]]:
		"""All group rows strictly under ``group`` (BFS, excludes ``group``)."""
		self._require_group(group)
		_, children_of, row_by_name = self._group_subtree_view(group)
		return self._rows_for(trees.descendants_of(group, children_of), row_by_name)

	def group_subtree(self, group: str) -> list[dict[str, Any]]:
		"""``group`` plus all its descendants (the full scoped subtree)."""
		self._require_group(group)
		_, children_of, row_by_name = self._group_subtree_view(group)
		return self._rows_for(trees.subtree_of(group, children_of), row_by_name)

	def group_path_to_root(self, group: str) -> list[dict[str, Any]]:
		"""``[group, parent_group, ..., branch-root group]`` — inclusive path."""
		self._require_group(group)
		parent_of, _, row_by_name = self._group_subtree_view(None)
		return self._rows_for(trees.path_to_root(group, parent_of), row_by_name)

	def group_tree(self) -> list[dict[str, Any]]:
		"""Every visible Flock Group row (whole tree). Permission-filtered by Frappe."""
		return [dict(r) for r in self._gw.fetch_group_subtree_rows(None)]

	# -- "member leads 1..N groups" (ADR-0001 §4.3) ------------------------- #
	def groups_led_by(self, member: str) -> list[dict[str, Any]]:
		"""Flock Group rows where ``leader == member`` (single-accountable leader).

		A member leading N groups is naturally expressed as N rows pointing at
		them (ADR §4.3). Use :meth:`groups_co_led_by` for the full
		Leader/Co-Leader roster.
		"""
		return [dict(r) for r in self._gw.fetch_groups_led_by(member)]

	def groups_co_led_by(self, member: str) -> list[dict[str, Any]]:
		"""Group rows where ``member`` holds a ``Leader``/``Co-Leader`` roster slot.

		Resolves the roster edges (``Flock Group Member``) to their parent
		``Flock Group`` rows and de-duplicates by group id — so a member who is
		both ``Flock Group.leader`` and a roster ``Co-Leader`` of the same group
		counts once.
		"""
		edges = self._gw.fetch_group_member_rows_for(member, LEADER_ROLES)
		seen: set[str] = set()
		out: list[dict[str, Any]] = []
		for edge in edges:
			group_name = edge.get("group")
			if not group_name or group_name in seen:
				continue
			row = self._gw.get_group(group_name)
			if row is None:
				continue
			seen.add(group_name)
			out.append(dict(row))
		return out

	def groups_led_or_co_led_by(self, member: str) -> list[dict[str, Any]]:
		"""Union of :meth:`groups_led_by` and :meth:`groups_co_led_by` (de-duped).

		The complete "leads 1..N groups" answer for a member: every group where
		they are the accountable leader, on the roster, or both. The basis for
		scoped fan-out and the leader-side of approval routing.
		"""
		by_id: dict[str, dict[str, Any]] = {r["name"]: dict(r) for r in self.groups_led_by(member)}
		for row in self.groups_co_led_by(member):
			by_id.setdefault(row["name"], row)
		return list(by_id.values())

	# -- approval-routing basis (ADR-0001 §6.6 / FLO-5 §4.6) ---------------- #
	def leader_chain_for_group(self, group: str) -> list[dict[str, Any]]:
		"""Leadership chain walking ``parent_group`` up to the branch-root group.

		Returns one ``{group, leader}`` entry per group in the path (leaf→root),
		skipping only groups whose ``leader`` is unset. This is the faithful
		tree-traversal input for [FLO-7](/FLO/issues/FLO-7) approval routing
		(ADR §6.6): the workflow — not this service — decides whether to dedupe
		a leader who appears at multiple levels (a leader approving their own
		request twice is a workflow concern, not a traversal concern). The
		branch-admin terminator and any "needs N levels" configurability also
		live in FLO-7 (ADR §6.6 delegates depth to FLO-7).
		"""
		self._require_group(group)
		parent_of, _, row_by_name = self._group_subtree_view(None)
		path = trees.path_to_root(group, parent_of)
		chain: list[dict[str, Any]] = []
		for node_name in path:
			row = row_by_name.get(node_name)
			if not row:
				continue
			leader = row.get("leader")
			if not leader:
				continue
			chain.append({"group": node_name, "leader": leader})
		return chain


# ---------------------------------------------------------------------------- #
# Module-level service accessor (lazy; production wires FrappeTreeReadGateway).
# ---------------------------------------------------------------------------- #
_service: TreeTraversalService | None = None


def get_service() -> TreeTraversalService:
	"""Process-wide service instance (lazily built, singleton per process)."""
	global _service
	if _service is None:
		_service = TreeTraversalService(FrappeTreeReadGateway())
	return _service


def install_gateway(gateway: TreeReadGateway) -> TreeTraversalService:
	"""Install a custom gateway (production wiring / tests) and return the service."""
	global _service
	_service = TreeTraversalService(gateway)
	return _service


# ---------------------------------------------------------------------------- #
# Internals — ``frappe.whitelist`` is only meaningful inside a running bench.
# At import time under plain ``pytest`` (CI gate) Frappe is absent, so we fall
# back to an identity decorator that keeps the function callable for unit tests
# of the transport layer (the service itself is exercised directly).
# ---------------------------------------------------------------------------- #
def frappe_whitelist():
	"""Return ``frappe.whitelist`` if Frappe is importable, else an identity deco."""
	try:
		import frappe

		return frappe.whitelist()
	except Exception:  # noqa: BLE001 - no bench under CI; the deco is a no-op

		def _identity(fn):  # type: ignore[no-untyped-def]
			return fn

		return _identity


def _throw_not_found(exc: FlockTreeError) -> None:
	import frappe

	frappe.throw(str(exc), title="Tree node not found")


# ---------------------------------------------------------------------------- #
# REST transport — thin ``@frappe.whitelist()`` wrappers (ADR §2 separation).
#
# Each endpoint validates the required arg, calls the service, and returns the
# row payload list. ``FlockTreeError`` (unknown node) is mapped to a 412 via
# ``frappe.throw`` so the client gets a structured error. The service itself
# never imports Frappe and is fully covered at project level.
# ---------------------------------------------------------------------------- #
def _service_for_request() -> TreeTraversalService:
	"""Resolve the service for a request (module-level; tests swap the gateway)."""
	return get_service()


def _require_arg(name: str, value: Any) -> str:
	if value is None or value == "":
		import frappe

		frappe.throw(f"{name} is required")
	return str(value)


def _serialize(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
	"""Ensure every value is JSON-serializable (Frappe marshals for us; defensive)."""
	return [{k: v for k, v in row.items() if v is not None} for row in rows]


@frappe_whitelist()
def get_branch_ancestors(branch: str) -> list[dict[str, Any]]:
	"""GET ``?branch=<name>`` → branch rows from parent up to the org root."""
	branch = _require_arg("branch", branch)
	try:
		return _serialize(_service_for_request().branch_ancestors(branch))
	except FlockTreeError as exc:
		_throw_not_found(exc)


@frappe_whitelist()
def get_branch_descendants(branch: str) -> list[dict[str, Any]]:
	"""GET ``?branch=<name>`` → all branch rows strictly under ``branch`` (BFS)."""
	branch = _require_arg("branch", branch)
	try:
		return _serialize(_service_for_request().branch_descendants(branch))
	except FlockTreeError as exc:
		_throw_not_found(exc)


@frappe_whitelist()
def get_branch_subtree(branch: str) -> list[dict[str, Any]]:
	"""GET ``?branch=<name>`` → ``branch`` plus all its descendants."""
	branch = _require_arg("branch", branch)
	try:
		return _serialize(_service_for_request().branch_subtree(branch))
	except FlockTreeError as exc:
		_throw_not_found(exc)


@frappe_whitelist()
def get_branch_path_to_root(branch: str) -> list[dict[str, Any]]:
	"""GET ``?branch=<name>`` → ``[branch, parent, ..., root]`` for breadcrumbs."""
	branch = _require_arg("branch", branch)
	try:
		return _serialize(_service_for_request().branch_path_to_root(branch))
	except FlockTreeError as exc:
		_throw_not_found(exc)


@frappe_whitelist()
def get_branch_tree() -> list[dict[str, Any]]:
	"""GET → every visible Flock Branch row (whole tree, permission-filtered)."""
	return _serialize(_service_for_request().branch_tree())


@frappe_whitelist()
def get_group_ancestors(group: str) -> list[dict[str, Any]]:
	"""GET ``?group=<name>`` → group rows from parent up to the branch-root group."""
	group = _require_arg("group", group)
	try:
		return _serialize(_service_for_request().group_ancestors(group))
	except FlockTreeError as exc:
		_throw_not_found(exc)


@frappe_whitelist()
def get_group_descendants(group: str) -> list[dict[str, Any]]:
	"""GET ``?group=<name>`` → all group rows strictly under ``group`` (BFS)."""
	group = _require_arg("group", group)
	try:
		return _serialize(_service_for_request().group_descendants(group))
	except FlockTreeError as exc:
		_throw_not_found(exc)


@frappe_whitelist()
def get_group_subtree(group: str) -> list[dict[str, Any]]:
	"""GET ``?group=<name>`` → ``group`` plus all its descendants."""
	group = _require_arg("group", group)
	try:
		return _serialize(_service_for_request().group_subtree(group))
	except FlockTreeError as exc:
		_throw_not_found(exc)


@frappe_whitelist()
def get_group_path_to_root(group: str) -> list[dict[str, Any]]:
	"""GET ``?group=<name>`` → ``[group, parent_group, ..., branch-root group]``."""
	group = _require_arg("group", group)
	try:
		return _serialize(_service_for_request().group_path_to_root(group))
	except FlockTreeError as exc:
		_throw_not_found(exc)


@frappe_whitelist()
def get_group_tree() -> list[dict[str, Any]]:
	"""GET → every visible Flock Group row (whole tree, permission-filtered)."""
	return _serialize(_service_for_request().group_tree())


@frappe_whitelist()
def get_groups_led_by_member(member: str) -> list[dict[str, Any]]:
	"""GET ``?member=<name>`` → Flock Group rows where ``leader == member``.

	See :meth:`TreeTraversalService.groups_led_or_co_led_by` for the full
	"leads 1..N groups" answer (accountable leader + roster).
	"""
	member = _require_arg("member", member)
	return _serialize(_service_for_request().groups_led_or_co_led_by(member))


@frappe_whitelist()
def get_leader_chain_for_group(group: str) -> list[dict[str, Any]]:
	"""GET ``?group=<name>`` → ordered ``[{group, leader}]`` chain up to the root.

	The tree-traversal input for [FLO-7](/FLO/issues/FLO-7) approval routing
	(ADR §6.6): each ancestor group's accountable leader, distinct, leaf→root.
	"""
	group = _require_arg("group", group)
	try:
		return _serialize(_service_for_request().leader_chain_for_group(group))
	except FlockTreeError as exc:
		_throw_not_found(exc)
