"""
Enterprise permission model — the single row-level-scoping chokepoint
(ADR-0001 §6, FLO-5 §4, [FLO-20](/FLO/issues/FLO-20)).

This module is the **only** place row-level scoping is computed. It composes
two axes (ADR §6.5 — "Tenant isolation is enforced once at the framework layer"):

* **Branch axis (native):** Frappe **User Permissions** on ``Flock Branch``.
  The subtree a ``Flock Branch Admin`` sees is *materialized* as a set of
  User-Permission rows (§6.2). The pure half lives here
  (:func:`compute_branch_subtree`); the Frappe half writes the UP rows
  (:class:`FrappeUserPermissionSyncer`).
* **Group-tree axis (custom):** **one** ``permission_query_conditions`` hook —
  :func:`get_group_scoped_conditions` — appends a single OR-fragment to the
  existing ``WHERE`` (§6.3). Nested-set predicate, leader scope keyed on stable
  group PKs, ``lft``/``rgt`` computed live.

Layering (ADR §2 separation of concerns)::

    Frappe permission_query_conditions hook  (transport, this module)
      -> resolve_user_scope(user)            (this module, domain service)
            |-> PermissionGateway port       (DB reads, hexagonal)
            |-> build_group_scope_sql(...)   (pure, no I/O — unit-tested)

The :class:`PermissionGateway` port wraps the only Frappe calls (user roles,
member resolution, leader-scope PKs, nested-set bounds, branch User
Permissions). Production: :class:`FrappePermissionGateway` (lazy Frappe import).
Unit tests: :class:`RecordingPermissionGateway`. This keeps the *shape* of the
scope (bypass vs leader vs self, OR-fragment, self-predication) fully unit-
testable without a bench — the same hexagonal discipline as
:mod:`flock_os.traversal` / :mod:`flock_os.events`.

Rules of engagement (ADR §6.5):

- Never check permissions in feature code via ad-hoc SQL; go through the hook or
  :func:`can` / :func:`assert_branch_scope` / :func:`assert_group_scope`.
- Never bypass ``permission_query_conditions`` with raw SQL; the only sanctioned
  escape hatch is :func:`system_query` (audited, explicit system context).
- Every tenant-scoped DocType is registered in :data:`SCOPED_DOCTYPES`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from flock_os.flock_os import trees
from flock_os.flock_os.trees import ChildMap, ParentMap

# ---------------------------------------------------------------------------- #
# System role catalog (mirrors flock_os.fixtures.FLOCK_ROLES — ADR §6.4).
#
# Centralized here so the permission layer never hard-codes a role string; the
# fixtures module owns seeding, this module owns the permission semantics.
# ---------------------------------------------------------------------------- #
ROLE_ORG_ADMIN = "Flock Org Admin"
ROLE_BRANCH_ADMIN = "Flock Branch Admin"
ROLE_GROUP_LEADER = "Flock Group Leader"
ROLE_MEMBER = "Flock Member"
ROLE_VISITOR = "Flock Visitor"
ROLE_AUDITOR = "Flock Auditor"

#: Roles that bypass the **group-axis** hook (ADR §6.3 — "broader scope wins").
#: Org Admin + Auditor see everything; Branch Admin is scoped natively by their
#: branch User Permissions (§6.2) and is intentionally NOT group-scoped — a
#: leader who is also Branch Admin is treated as Branch Admin.
BYPASS_ROLES: frozenset[str] = frozenset({ROLE_ORG_ADMIN, ROLE_BRANCH_ADMIN, ROLE_AUDITOR})

#: Roles that see **every branch** on the branch axis (ADR §6.2). Org Admin +
#: Auditor carry no branch User Permission. Note: Branch Admin is NOT here — it
#: is the very role the branch axis scopes (to its subtree). The two axes have
#: different bypass sets by design (§6.2 vs §6.3).
GLOBAL_BRANCH_ROLES: frozenset[str] = frozenset({ROLE_ORG_ADMIN, ROLE_AUDITOR})

#: Roles whose row-level scope is narrowed by the group-axis hook.
GROUP_SCOPED_ROLES: frozenset[str] = frozenset({ROLE_GROUP_LEADER, ROLE_MEMBER, ROLE_VISITOR})

# ---------------------------------------------------------------------------- #
# SCOPED_DOCTYPES — the single list both the group-axis hook and the audit/QA
# gate consult (ADR §6.5 — "one list, not per-DocType methods").
#
# Every tenant-scoped DocType that carries a `group` link (or IS `Flock Group`)
# is registered here. Phase 1 ships the group-tree subset; transactional
# DocTypes (gatherings, attendance, registrations) append themselves as their
# features land — the hook reads this list, so no per-DocType method is needed.
# ---------------------------------------------------------------------------- #
SCOPED_DOCTYPES: tuple[str, ...] = (
	"Flock Group",
	"Flock Group Member",
	"Flock Gathering",
	"Flock Announcement",
	"Flock Event Approval",
	"Flock Event Registration",
	"Flock Event Invitation",
)
"""DocTypes the group-axis ``permission_query_conditions`` hook narrows.

Adding a group-level DocType = appending its name here + wiring the hook in
``hooks.py`` (already driven from this list, so no per-DocType wiring). The
self-predication special case (``Flock Group``) is handled inside
:func:`build_group_scope_sql` (ADR §6.3 edit #2).

DocTypes whose ``group`` link is **nullable** (e.g. ``Flock Announcement``,
primarily branch-subtree-scoped — FLO-8 §6.2) are supported via the NULL-
``group`` passthrough in :func:`build_group_scope_sql`: rows whose ``group`` is
NULL fall through to the branch axis (native User Permissions), rows with a
group get the nested-set predicate. Required-``group`` DocTypes
(``Flock Group Member``) never hit the passthrough — the ``group IS NULL``
clause is a no-op for them.

Not every scoped DocType carries a ``member`` link. The self-membership branch
of the OR-fragment (``.member = <leader_member>``, edit #4) only applies to
:data:`MEMBER_ANCHORED_DOCTYPES` (rows that are *about* a person — a membership
edge, an attendance record). Group-only DocTypes like ``Flock Gathering`` (a
meeting owned by a group, not a person) must not get a ``.member`` clause — they
have no such column, so emitting one would yield invalid SQL (FLO-54)."""

#: Scoped DocTypes that carry a member-like link the self-membership branch
#: (edit #4) predicates on. A row that is *about* a person (a membership edge,
#: an attendance record, a registration) lets a member see their own rows via
#: ``.<column> = self``. Group-only DocTypes (``Flock Gathering``) are
#: intentionally absent — they have no such column, so the ``.member`` clause
#: is suppressed for them (FLO-54). Mapped doctype → the exact member-column
#: name on that table (``member`` for ``Flock Group Member``; ``registrant`` for
#: ``Flock Event Registration`` per FLO-7 §3.5; ``invitee`` for ``Flock Event
#: Invitation`` per FLO-7 §3.6) so the emitted SQL matches each table's real
#: schema. ``Flock Attendance Record`` appends itself here when it gains its
#: member link.
MEMBER_ANCHORED_DOCTYPES: dict[str, str] = {
	"Flock Group Member": "member",
	"Flock Event Registration": "registrant",
	"Flock Event Invitation": "invitee",
}

#: The branch doctype the native User-Permission axis rides on (ADR §6.2).
BRANCH_DOCTYPE = "Flock Branch"
#: The group doctype whose nested set the custom hook predicates on (ADR §6.3).
GROUP_DOCTYPE = "Flock Group"
#: The leadership roster roles whose union forms a leader's led scope (§4.3).
LEADER_ROSTER_ROLES: tuple[str, ...] = ("Leader", "Co-Leader")


# ---------------------------------------------------------------------------- #
# Leader scope — the resolved, role-aware view of what a user can see on the
# group axis (ADR §6.3). Kept as plain data so the SQL builder stays pure.
# ---------------------------------------------------------------------------- #
@dataclass(frozen=True)
class GroupBounds:
	"""One led/joined group's live nested-set bounds (computed at read time)."""

	name: str
	lft: int
	rgt: int


@dataclass(frozen=True)
class LeaderScope:
	"""A user's resolved group-axis scope (ADR §6.3 — keyed on stable PKs).

	``led_bounds`` are the **live** ``lft``/``rgt`` of each led group, looked up
	at read time — the cache stores PKs only, so reparent correctness never
	depends on cache freshness (ADR §6.3 edit #3). ``joined_groups`` are groups
	the user merely belongs to (self-membership / read scope).
	"""

	member: str | None
	led_bounds: tuple[GroupBounds, ...] = field(default_factory=tuple)
	joined_groups: tuple[str, ...] = field(default_factory=tuple)

	@property
	def is_leader(self) -> bool:
		"""True iff this user leads at least one group (subtree scope applies)."""
		return bool(self.led_bounds)


# ---------------------------------------------------------------------------- #
# Gateway port (hexagonal) — the only Frappe-touching surface in this module.
# ---------------------------------------------------------------------------- #
@runtime_checkable
class PermissionGateway(Protocol):
	"""Port: the DB reads the permission layer needs (ADR §6.2 / §6.3).

	Production adapter: :class:`FrappePermissionGateway` (lazy Frappe import).
	Unit tests: :class:`RecordingPermissionGateway`. Returns plain data so the
	scope resolver + SQL builder stay Frappe-agnostic and transport-agnostic.
	"""

	def get_user_roles(self, user: str) -> frozenset[str]:
		"""The Frappe Roles assigned to ``user`` (incl. System Manager etc.)."""
		...

	def resolve_member_for_user(self, user: str) -> str | None:
		"""The ``Flock Member.name`` linked to ``user`` (``linked_user``), or ``None``.

		A user with no linked member has no group-axis self-scope (§4.3).
		"""
		...

	def fetch_led_group_bounds(self, member: str) -> tuple[GroupBounds, ...]:
		"""Live nested-set bounds of every group the member leads.

		Led scope = ``Flock Group.leader = member`` ∪ ``Flock Group Member(role ∈
		Leader/Co-Leader)`` (ADR §4.3), de-duplicated by group PK. ``lft``/``rgt``
		are read **live** from the current nested set (zero staleness window).
		"""
		...

	def fetch_joined_group_names(self, member: str) -> tuple[str, ...]:
		"""Groups ``member`` merely belongs to (any roster role), de-duplicated.

		Drives the self-membership / read branch of the OR-fragment for non-led
		groups (members see the groups they belong to, §4.1).
		"""
		...

	def fetch_group_bounds(self, name: str) -> GroupBounds | None:
		"""Live nested-set bounds (``lft``/``rgt``) of a single group, or ``None``.

		Used by :func:`assert_group_scope` to test whether a row's group anchor
		falls inside a leader's led subtree (the dual of the nested-set predicate
		the ``permission_query_conditions`` hook applies, ADR §6.3), so the guard
		and the list hook agree — a leader of an ancestor group may operate on any
		descendant row, not just the groups they directly lead.
		"""
		...

	def list_branch_user_permissions(self, user: str) -> tuple[str, ...]:
		"""Branch names the user holds a ``Flock Branch`` User Permission for.

		Native branch axis (§6.2): empty for Org Admin/Auditor (see all) and for
		members/visitors (self-scoped, not branch-scoped); populated for Branch
		Admins + Group Leaders (their home branch(es)).
		"""
		...


class NullPermissionGateway:
	"""Empty gateway — the default before production wiring; yields no scope."""

	def get_user_roles(self, user: str) -> frozenset[str]:  # noqa: ARG002
		return frozenset()

	def resolve_member_for_user(self, user: str) -> str | None:  # noqa: ARG002
		return None

	def fetch_led_group_bounds(self, member: str) -> tuple[GroupBounds, ...]:  # noqa: ARG002
		return ()

	def fetch_joined_group_names(self, member: str) -> tuple[str, ...]:  # noqa: ARG002
		return ()

	def fetch_group_bounds(self, name: str) -> GroupBounds | None:  # noqa: ARG002
		return None

	def list_branch_user_permissions(self, user: str) -> tuple[str, ...]:  # noqa: ARG002
		return ()


# ---------------------------------------------------------------------------- #
# Pure scope resolver — composes the gateway reads into a :class:`LeaderScope`.
# ---------------------------------------------------------------------------- #
def resolve_leader_scope(
	gateway: PermissionGateway, *, user: str, roles: Sequence[str] | None = None
) -> LeaderScope:
	"""Resolve a user's group-axis scope (ADR §6.3).

	``roles`` defaults to the gateway lookup. Bypass roles short-circuit to an
	empty (no-op) scope — the caller (:func:`get_group_scoped_conditions`) treats
	empty + bypass as "no fragment", and :func:`can` treats bypass as allow.
	"""
	user_roles = frozenset(roles) if roles is not None else gateway.get_user_roles(user)
	if user_roles & BYPASS_ROLES:
		return LeaderScope(member=gateway.resolve_member_for_user(user))
	member = gateway.resolve_member_for_user(user)
	if member is None:
		return LeaderScope(member=None)
	return LeaderScope(
		member=member,
		led_bounds=gateway.fetch_led_group_bounds(member),
		joined_groups=gateway.fetch_joined_group_names(member),
	)


# ---------------------------------------------------------------------------- #
# Pure SQL fragment builder — THE custom mechanism (ADR §6.3).
#
# Produces a single OR-fragment appended to the existing WHERE (never a UNION —
# `permission_query_conditions` may only append). Fully unit-testable: takes the
# resolved scope + an `escape` callable (production passes `frappe.db.escape` so
# PK values are quoted safely; tests pass a passthrough).
# ---------------------------------------------------------------------------- #
def _esc(value: Any, escape) -> str:  # type: ignore[no-untyped-def]
	"""Quote/escape a scalar for inline SQL (production: ``frappe.db.escape``)."""
	return str(escape(value))


def _in_list(values: Sequence[str], escape) -> str:  # type: ignore[no-untyped-def]
	"""Render a SQL ``IN ('a', 'b')`` literal from escaped string values."""
	return "(" + ", ".join(_esc(v, escape) for v in values) + ")"


def build_group_scope_sql(
	*,
	doctype: str,
	scope: LeaderScope,
	escape,  # type: ignore[no-untyped-def]
) -> str:
	"""Build the group-axis ``WHERE`` fragment for ``doctype`` (ADR §6.3).

	Returns ``""`` when the scope adds no restriction (bypass roles, or a user
	with no linked member — Frappe's DocPerm layer still applies). Otherwise
	returns a single ``AND ( ... )`` OR-fragment. For ``Flock Group`` it narrows
	on the doctype's **own nested set** (self-predication, edit #2) plus the
	groups the user belongs to by name. For every other scoped DocType it
	narrows via the ``.group`` link (subtree of led groups) **OR** ``.group IN
	(<joined groups>)``; nullable-``group`` DocTypes additionally OR ``.group IS
	NULL`` (passthrough to the branch axis); DocTypes in
	:data:`MEMBER_ANCHORED_DOCTYPES` (rows about a person) additionally OR
	``.member = <leader_member>`` (self-membership, edit #4). The ``.member``
	clause is suppressed for group-only DocTypes (e.g. ``Flock Gathering``) —
	they have no such column (FLO-54).

	The subtree sub-select OR-composes one ``(lft, rgt)`` range per led group
	(computed live), so disjoint led groups do not over-select siblings.
	"""
	if not scope.member and not scope.led_bounds and not scope.joined_groups:
		return ""

	is_self = doctype == GROUP_DOCTYPE

	# The live nested-set range predicate over led groups, OR-composed per group
	# so disjoint led subtrees don't over-select siblings (ADR §6.3). Empty when
	# the user leads nothing → the subtree branch matches no rows.
	if scope.led_bounds:
		range_clause = " OR ".join(
			f"`tabFlock Group`.`lft` >= {b.lft} AND `tabFlock Group`.`rgt` <= {b.rgt}"
			for b in scope.led_bounds
		)
		subtree_subselect = f"SELECT name FROM `tabFlock Group` WHERE {range_clause}"
	else:
		subtree_subselect = "SELECT name FROM `tabFlock Group` WHERE 0"

	branches: list[str] = []
	if is_self:
		# Self-predication (edit #2): predicate on Flock Group's own nested set.
		branches.append(f"`tabFlock Group`.`name` IN ({subtree_subselect})")
		if scope.joined_groups:
			branches.append(f"`tabFlock Group`.`name` IN {_in_list(scope.joined_groups, escape)}")
	else:
		alias = f"`tab{doctype}`"
		# Nullable-group passthrough (FLO-8 §6.2 / edit #5): DocTypes whose
		# `group` link is optional (e.g. `Flock Announcement`, primarily branch-
		# subtree-scoped) let rows with NULL `group` fall through to the branch
		# axis (native User Permissions) — the group-axis hook must not hide
		# them. Required-`group` DocTypes (`Flock Group Member`, `Flock Gathering`)
		# never have a NULL `group`, so this clause is a harmless no-op for them.
		branches.append(f"{alias}.`group` IS NULL")
		branches.append(f"{alias}.`group` IN ({subtree_subselect})")
		# Self-membership (edit #4) applies ONLY to DocTypes that carry a
		# member-like link (rows about a person). Group-only DocTypes (e.g. `Flock
		# Gathering`, `Flock Announcement`) have no such column, so the
		# clause is suppressed — emitting it would produce invalid SQL. See
		# MEMBER_ANCHORED_DOCTYPES (doctype → the exact member-column name).
		if scope.member and doctype in MEMBER_ANCHORED_DOCTYPES:
			member_col = MEMBER_ANCHORED_DOCTYPES[doctype]
			branches.append(f"{alias}.`{member_col}` = {_esc(scope.member, escape)}")
		if scope.joined_groups:
			branches.append(f"{alias}.`group` IN {_in_list(scope.joined_groups, escape)}")

	return " AND (" + " OR ".join(branches) + ")"


def build_group_scope_sql_safe(*, doctype: str, scope: LeaderScope) -> str:
	"""Production-safe wrapper that escapes values via ``frappe.db.escape``.

	Lazy Frappe import so this module stays import-clean under plain pytest.
	"""
	import frappe

	return build_group_scope_sql(doctype=doctype, scope=scope, escape=frappe.db.escape)


# ---------------------------------------------------------------------------- #
# Branch-axis subtree materialization (ADR §6.2).
#
# The pure half: given a branch tree adjacency, compute the descendant branch
# set a Branch Admin must see (themselves + subtree). Reuses the pure traversal
# primitives in `flock_os.flock_os.trees` (DRY). The Frappe half writes/removes
# the User-Permission rows (FrappeUserPermissionSyncer below).
# ---------------------------------------------------------------------------- #
def compute_branch_subtree(branch: str, *, parent_of: ParentMap, children_of: ChildMap) -> tuple[str, ...]:
	"""Branch + its descendants — the UP rows a Branch Admin's subtree covers.

	The native Frappe User-Permission machinery is equality-based per node, so a
	regional admin's subtree is *materialized* as one UP row per descendant
	branch (ADR §6.2). Ordered root→leaf (BFS) for deterministic seeding.
	"""
	if not branch:
		raise trees.FlockTreeError("branch is required to compute its admin subtree.")
	return tuple(trees.subtree_of(branch, children_of))


def compute_branch_subtree_rebuild_target(moved_branch: str, *, parent_of: ParentMap) -> str:
	"""The branch whose admin subtree must re-sync after ``moved_branch`` moved.

	On ``flock.branch.moved`` the only admin scopes that can change are those of
	an **ancestor** of the moved branch (the moved subtree's membership shifted).
	Returns the moved branch's root — the broadest scope that might need a
	re-sync; the syncer re-materializes every admin whose allowed-set intersects.
	"""
	return trees.root_of(moved_branch, parent_of)


# ---------------------------------------------------------------------------- #
# Scope decisions + guards (ADR §6.5 — the single sanctioned permission API).
# ---------------------------------------------------------------------------- #
def is_bypass_user(roles: Sequence[str]) -> bool:
	"""True iff any role on ``roles`` bypasses the group-axis hook (§6.3)."""
	return bool(frozenset(roles) & BYPASS_ROLES)


def can_access_branch(*, branch: str | None, allowed_branches: Sequence[str], roles: Sequence[str]) -> bool:
	"""True iff the user may read/write rows anchored at ``branch`` (§6.2).

	Global-branch roles (Org Admin / Auditor) see all branches. A Branch Admin /
	Group Leader sees ``branch`` iff it is in their materialized allowed set
	(theirs is *not* a global role — they are the scope target of this axis).
	A null ``branch`` (org-wide rows like an org-scoped announcement) is visible
	to any authenticated user with a DocPerm — branch scoping does not apply.
	"""
	if frozenset(roles) & GLOBAL_BRANCH_ROLES:
		return True
	if not branch:
		return True
	return branch in set(allowed_branches)


def assert_branch_scope(*, doc_branch: str | None, user: str, gateway: PermissionGateway) -> None:
	"""Guard: raise :class:`FlockPermissionError` if ``user`` lacks branch scope.

	The single sanctioned branch-scope assertion every ``@frappe.whitelist()``
	endpoint calls before returning cross-branch data (ADR §6.5 / §4.7 #3).
	"""
	roles = gateway.get_user_roles(user)
	allowed = gateway.list_branch_user_permissions(user)
	if can_access_branch(branch=doc_branch, allowed_branches=allowed, roles=roles):
		return
	raise FlockPermissionError(
		f"User {user!r} lacks branch scope for {doc_branch!r} (not in their allowed branches)."
	)


def assert_group_scope(
	*, doc_group: str | None, doc_member: str | None, user: str, gateway: PermissionGateway
) -> None:
	"""Guard: raise if ``user`` lacks group-axis scope over the given row.

	Bypass roles pass. Otherwise the row's ``group`` must fall inside the user's
	led subtree or joined set, or the row's ``member`` must be the user's own
	member (self-membership, §4.3 edit #4).
	"""
	roles = gateway.get_user_roles(user)
	if is_bypass_user(roles):
		return
	scope = resolve_leader_scope(gateway, user=user, roles=roles)
	if doc_group is None and doc_member is None:
		# Nothing group-axis to check; DocPerm / branch axis own this row.
		return
	if scope.member and doc_member and doc_member == scope.member:
		return
	if doc_group:
		# Subtree containment (ADR §6.3): a leader of an ancestor group may
		# operate on any descendant row. This mirrors the nested-set predicate
		# the ``permission_query_conditions`` hook applies (the dual of
		# ``trees.is_descendant_of``), so the guard and the list hook agree —
		# previously this checked direct membership only and denied a leader's
		# call on a subtree-descendant row the list view let them see.
		doc_bounds = gateway.fetch_group_bounds(doc_group)
		if doc_bounds and any(
			led.lft <= doc_bounds.lft and doc_bounds.rgt <= led.rgt for led in scope.led_bounds
		):
			return
		# Direct joined-group membership (self read scope, §4.1).
		if doc_group in set(scope.joined_groups):
			return
	raise FlockPermissionError(
		f"User {user!r} lacks group-axis scope for group={doc_group!r} member={doc_member!r}."
	)


# ---------------------------------------------------------------------------- #
# Approval-authority guard (FLO-7 §4.2 / §6.2 — `assert_approval_scope`).
#
# The custom guard that makes "approved up the tree by the scoped leaders" real:
# a user may decide an approval step only if they are that step's resolved
# approver AND the gathering's group lies in their led subtree AND they share
# the gathering's branch. This is intentionally NOT a bypass-role short-circuit
# — approval authority is chain membership, so even an Org Admin who is not the
# resolved approver cannot decide someone else's step (DoD #4: "a non-chain
# leader, or a cross-branch user, cannot approve/reject"). The step is taken as
# a structural type (duck-typed attrs) so this module need not import
# :mod:`flock_os.approvals` — avoiding an import cycle (the approval actions
# import this guard lazily).
# ---------------------------------------------------------------------------- #
#: The approver-level label for the terminal Branch-Admin step (FLO-7 §3.3).
APPROVAL_LEVEL_BRANCH_ADMIN = "Branch Admin"


def can_decide_approval_step(
	*,
	step,
	user: str,
	gateway: PermissionGateway,  # type: ignore[valid-type]
) -> bool:
	"""True iff ``user`` may approve/reject this approval step (§4.2 / §6.2).

	Three checks, all must pass. Identity: ``user`` is the step's resolved
	approver (``approver_user`` matches, or ``approver_member`` matches the
	user's linked ``Flock Member``); for a Branch-Admin step, any ``Flock Branch
	Admin`` scoped to the step's branch qualifies (the terminator is a role, not
	a single person). Branch axis: the step's ``doc_branch`` is in the user's
	allowed branch set (or the user is a global-branch role). Group axis: for a
	non-Branch-Admin step, the step's ``doc_group`` falls inside the user's led
	subtree (the dual of the nested-set predicate the
	``permission_query_conditions`` hook applies), so a leader of an ancestor
	group may decide a descendant gathering's step; bypass roles skip this axis.
	"""
	roles = gateway.get_user_roles(user)
	member = gateway.resolve_member_for_user(user)
	allowed_branches = gateway.list_branch_user_permissions(user)
	doc_branch = getattr(step, "doc_branch", None)
	doc_group = getattr(step, "doc_group", None)

	# 1. Identity.
	is_resolved_approver = False
	approver_user = getattr(step, "approver_user", None)
	approver_member = getattr(step, "approver_member", None)
	if approver_user and approver_user == user:
		is_resolved_approver = True
	if approver_member and member and approver_member == member:
		is_resolved_approver = True
	if getattr(step, "approver_level", None) == APPROVAL_LEVEL_BRANCH_ADMIN:
		# The terminal step is a role: any Branch Admin scoped to the branch.
		if ROLE_BRANCH_ADMIN in roles and can_access_branch(
			branch=doc_branch, allowed_branches=allowed_branches, roles=roles
		):
			is_resolved_approver = True
	if not is_resolved_approver:
		return False

	# 2. Branch axis.
	if not can_access_branch(branch=doc_branch, allowed_branches=allowed_branches, roles=roles):
		return False

	# 3. Group axis (subtree containment) — skipped for the branch-admin
	# terminator (no group) and for bypass roles (scoped natively).
	if doc_group and getattr(step, "approver_level", None) != APPROVAL_LEVEL_BRANCH_ADMIN:
		if is_bypass_user(roles):
			return True
		doc_bounds = gateway.fetch_group_bounds(doc_group)
		if doc_bounds is None:
			return False
		scope = resolve_leader_scope(gateway, user=user, roles=roles)
		return any(led.lft <= doc_bounds.lft and doc_bounds.rgt <= led.rgt for led in scope.led_bounds)
	return True


def assert_approval_scope(*, step, user: str, gateway: PermissionGateway) -> None:  # type: ignore[valid-type]
	"""Guard: raise :class:`FlockPermissionError` if ``user`` may not decide ``step``.

	The single sanctioned approval-authority assertion every approval
	``@frappe.whitelist()`` endpoint calls before applying a decision (§6.2).
	"""
	if can_decide_approval_step(step=step, user=user, gateway=gateway):
		return
	raise FlockPermissionError(
		f"User {user!r} is not the resolved approver for this step "
		f"(member={getattr(step, 'approver_member', None)!r}) or lacks scope "
		f"(branch={getattr(step, 'doc_branch', None)!r}, group={getattr(step, 'doc_group', None)!r})."
	)


class FlockPermissionError(PermissionError):
	"""Raised when a row-level scope assertion fails (ADR §6.5)."""


# ---------------------------------------------------------------------------- #
# System escape hatch — audited, explicit (ADR §6.5).
#
# Reporting / bulk paths that must read across scopes call this single helper
# instead of scattering ``ignore_permissions=True``. Every call is logged to
# ``Flock Audit Log`` so cross-scope reads are auditable (§4.7 #2).
# ---------------------------------------------------------------------------- #
def system_query(
	gateway: SystemQueryGateway,
	*,
	doctype: str,
	filters: dict[str, Any] | None = None,
	fields: Sequence[str] | None = None,
	reason: str,
	actor: str = "System",
) -> list[dict[str, Any]]:
	"""Audited cross-scope read (ADR §6.5 / §4.7 #2).

	Runs a permission-bypassing ``get_all`` and records the access to
	``Flock Audit Log`` with ``reason``. ``reason`` is mandatory — every
	cross-scope read must justify itself. Returns plain rows.
	"""
	results = gateway.system_get_all(doctype, filters=filters or {}, fields=list(fields or ["name"]))
	gateway.audit(action="system_query", doctype=doctype, actor=actor, reason=reason, detail=str(filters))
	return results


@runtime_checkable
class SystemQueryGateway(Protocol):
	"""Port: the audited system-read surface (production: Frappe)."""

	def system_get_all(
		self, doctype: str, *, filters: dict[str, Any], fields: list[str]
	) -> list[dict[str, Any]]:
		"""Permission-bypassing list read (``frappe.get_all(ignore_permissions=True)``)."""
		...

	def audit(self, *, action: str, doctype: str, actor: str, reason: str, detail: str) -> None:
		"""Append a row to ``Flock Audit Log`` (best-effort; never raises)."""
		...


class FrappeSystemQueryGateway:
	"""Production adapter for the audited cross-scope read (ADR §6.5 / §4.7 #2).

	The only place ``ignore_permissions=True`` is sanctioned. Every call writes a
	``Flock Audit Log`` row so cross-scope reads are auditable by ``Flock
	Auditor``. The audit write is best-effort (never fails the originating read).
	"""

	AUDIT_DOCTYPE = "Flock Audit Log"

	@property
	def _frappe(self):  # type: ignore[no-untyped-def]
		import frappe

		return frappe

	def system_get_all(
		self, doctype: str, *, filters: dict[str, Any], fields: list[str]
	) -> list[dict[str, Any]]:
		frappe = self._frappe
		return frappe.get_all(doctype, filters=filters, fields=fields, ignore_permissions=True)

	def audit(self, *, action: str, doctype: str, actor: str, reason: str, detail: str) -> None:
		frappe = self._frappe
		try:
			frappe.get_doc(
				{
					"doctype": self.AUDIT_DOCTYPE,
					"action": action,
					"doctype_ref": doctype,
					"actor": actor,
					"reason": reason,
					"detail": detail,
				}
			).db_insert()
		except Exception:  # noqa: BLE001 - audit is best-effort (never breaks the read)
			frappe.log_error(f"flock_os.permissions.audit failed: {action} {doctype}")


# ---------------------------------------------------------------------------- #
# Frappe adapters (lazy import — this module stays import-clean under pytest).
# ---------------------------------------------------------------------------- #
class FrappePermissionGateway:
	"""Production adapter over ``frappe.get_all`` / ``frappe.db.get_value``.

	All reads except the leader-scope bounds go through ``frappe.get_all`` so the
	caller's role + User Permissions apply automatically (ADR §6.2). The
	leader-scope resolution reads ``lft``/``rgt`` live from the nested set.
	"""

	@property
	def _frappe(self):  # type: ignore[no-untyped-def]
		import frappe

		return frappe

	def get_user_roles(self, user: str) -> frozenset[str]:
		frappe = self._frappe
		return frozenset(frappe.get_roles(user))

	def resolve_member_for_user(self, user: str) -> str | None:
		frappe = self._frappe
		return frappe.db.get_value("Flock Member", {"linked_user": user}, "name") if user else None

	def fetch_led_group_bounds(self, member: str) -> tuple[GroupBounds, ...]:
		frappe = self._frappe
		if not member:
			return ()
		# Led scope = leader field ∪ roster Leader/Co-Leader edges (ADR §4.3).
		leader_rows = frappe.get_all("Flock Group", filters={"leader": member}, pluck="name")
		roster_rows = frappe.get_all(
			"Flock Group Member",
			filters={"member": member, "role": ["in", list(LEADER_ROSTER_ROLES)]},
			pluck="group",
		)
		group_names: dict[str, None] = dict.fromkeys([*leader_rows, *roster_rows])
		if not group_names:
			return ()
		return self._bounds_for(tuple(group_names))

	def fetch_joined_group_names(self, member: str) -> tuple[str, ...]:
		frappe = self._frappe
		if not member:
			return ()
		rows = frappe.get_all("Flock Group Member", filters={"member": member}, pluck="group")
		# Preserve order, de-dup.
		seen: dict[str, None] = dict.fromkeys(rows)
		return tuple(seen)

	def fetch_group_bounds(self, name: str) -> GroupBounds | None:
		frappe = self._frappe
		if not name:
			return None
		bounds = frappe.db.get_value(GROUP_DOCTYPE, name, ["lft", "rgt"])
		if not bounds:
			return None
		lft, rgt = bounds
		return GroupBounds(name=name, lft=int(lft), rgt=int(rgt))

	def list_branch_user_permissions(self, user: str) -> tuple[str, ...]:
		frappe = self._frappe
		if not user:
			return ()
		rows = frappe.get_all(
			"User Permission",
			filters={"user": user, "allow": BRANCH_DOCTYPE},
			pluck="for_value",
		)
		return tuple(rows)

	def _bounds_for(self, group_names: tuple[str, ...]) -> tuple[GroupBounds, ...]:
		out: list[GroupBounds] = []
		for name in group_names:
			bounds = self.fetch_group_bounds(name)
			if bounds:
				out.append(bounds)
		return tuple(out)


class FrappeUserPermissionSyncer:
	"""Production branch-axis UP writer (ADR §6.2).

	Materializes a Branch Admin's subtree as one ``User Permission(Branch=<n>)``
	row per descendant branch, and removes stale rows on re-sync / revoke. The
	native Frappe path stays authoritative — this only seeds the equality rows
	Frappe's filter needs.
	"""

	USER_PERM_DOCTYPE = "User Permission"

	@property
	def _frappe(self):  # type: ignore[no-untyped-def]
		import frappe

		return frappe

	def sync_branch_scope(
		self, *, user: str, branch_subtree: Sequence[str], organization: str | None = None
	) -> None:
		"""Reconcile ``user``'s Flock-Branch User Permissions to ``branch_subtree``.

		Adds missing rows, removes extras (so a move that drops a branch from the
		admin's subtree is reflected). Idempotent.
		"""
		frappe = self._frappe
		target = set(branch_subtree)
		existing = set(
			frappe.get_all(
				self.USER_PERM_DOCTYPE,
				filters={"user": user, "allow": BRANCH_DOCTYPE},
				pluck="for_value",
			)
		)
		for branch in target - existing:
			frappe.get_doc(
				{
					"doctype": self.USER_PERM_DOCTYPE,
					"user": user,
					"allow": BRANCH_DOCTYPE,
					"for_value": branch,
					"app_for_organization": organization,
				}
			).insert(ignore_permissions=True)
		for branch in existing - target:
			for name in frappe.get_all(
				self.USER_PERM_DOCTYPE,
				filters={"user": user, "allow": BRANCH_DOCTYPE, "for_value": branch},
				pluck="name",
			):
				frappe.delete_doc(self.USER_PERM_DOCTYPE, name, ignore_permissions=True)


# ---------------------------------------------------------------------------- #
# Module-level service accessor (lazy; production wires FrappePermissionGateway).
# ---------------------------------------------------------------------------- #
_gateway: PermissionGateway | None = None


def get_gateway() -> PermissionGateway:
	"""Process-wide permission gateway (lazily built, singleton per process)."""
	global _gateway
	if _gateway is None:
		_gateway = FrappePermissionGateway()
	return _gateway


def install_gateway(gateway: PermissionGateway) -> PermissionGateway:
	"""Install a custom gateway (production wiring / tests) and return it."""
	global _gateway
	_gateway = gateway
	return _gateway


# ---------------------------------------------------------------------------- #
# permission_query_conditions hook (ADR §6.3) — the transport entry point.
#
# Registered in hooks.py for every name in SCOPED_DOCTYPES. Returns "" for the
# bypass / no-scope cases (Frappe composes a no-op); returns the OR-fragment for
# a leader/member. Frappe dispatches the hook as ``frappe.call(method, user,
# doctype=doctype)`` — ``user`` positional, ``doctype`` keyword
# (apps/frappe/frappe/model/db_query.py:1130); the signature matches that
# convention. Under plain pytest it is exercised directly against a
# RecordingPermissionGateway.
# ---------------------------------------------------------------------------- #
def get_group_scoped_conditions(user: str | None = None, doctype: str | None = None) -> str:
	"""``permission_query_conditions`` hook — the one custom group-axis mechanism.

	ADR §6.3: appends a single OR-fragment to the existing ``WHERE``. Bypass
	roles (Org Admin / Auditor / Branch Admin) and users with no resolved scope
	get ``""`` (no-op). Leaders/members get their nested-set + self/joined
	predicate. Values are escaped via ``frappe.db.escape``.

	Under plain pytest (no bench) the session user + escape fall back to a
	passthrough so the hook's *contract* is unit-testable against an installed
	:class:`RecordingPermissionGateway` (same discipline as
	:func:`flock_os.traversal.frappe_whitelist`).
	"""
	if doctype not in SCOPED_DOCTYPES:
		return ""
	resolved_user = user or _session_user()
	gateway = get_gateway()
	roles = gateway.get_user_roles(resolved_user)
	# §6.3 bypass: Org Admin / Auditor / Branch Admin are not group-scoped (a
	# leader who is also Branch Admin is treated as Branch Admin — broader wins).
	if roles & BYPASS_ROLES:
		return ""
	scope = resolve_leader_scope(gateway, user=resolved_user, roles=roles)
	if not scope.member and not scope.led_bounds and not scope.joined_groups:
		return ""
	return build_group_scope_sql(doctype=doctype, scope=scope, escape=_db_escape())


def _session_user() -> str:
	"""Current Frappe session user, or ``""`` when Frappe is unavailable (CI)."""
	try:
		import frappe

		return frappe.session.user
	except Exception:  # noqa: BLE001 - no bench under CI
		return ""


def _db_escape():
	"""``frappe.db.escape`` for safe SQL quoting, or ``str`` when Frappe is absent."""
	try:
		import frappe

		return frappe.db.escape
	except Exception:  # noqa: BLE001 - no bench under CI
		return str


def has_group_scope(doctype: str, user: str) -> bool:
	"""Whether ``get_group_scoped_conditions`` would emit a non-empty fragment.

	Exposed for the QA gate ([FLO-21](/FLO/issues/FLO-21)) and audit asserts so a
	test can confirm leader scoping actually fires on the group list (the §6.3
	self-predication regression risk).
	"""
	return bool(get_group_scoped_conditions(user=user, doctype=doctype).strip())
