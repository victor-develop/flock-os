"""
Scoped push fan-out service — Admin → leader notifications (FLO-57, FLO-8 §6.1).

One concern: resolve the **leader audience** for an org-tree scope (a branch
subtree or a group subtree) and deliver a push notification to them — reusing
the FLO-14 realtime fan-out and emitting ``flock.notification.sent`` through the
single canonical emitter. No second fan-out mechanism is introduced.

Layering (ADR-0001 §2 separation of concerns)::

    Announcement / admin controller   (Frappe transport, FLO-55 / FLO-8 §6)
      -> ScopedNotificationService.fanout(...)   <- THIS module, domain service
            |-> NotificationFanoutGateway port   (DB reads: leaders + tree, hexagonal)
            |-> resolve_scoped_audience(...)      (pure: subtree + leaders, no I/O)
            |-> RealtimePublisher.publish(...)    (REUSE FLO-14 — the sanctioned realtime path)
            |-> events.emit(NOTIFICATION_SENT)    (canonical emitter — audit/outbox)

Reuse contract — FLO-57 DoD *"reuses the FLO-14 sharded realtime fan-out
(no duplication)"*:

* The **same** :class:`flock_os.realtime.RealtimePublisher` port FLO-14's
  projector publishes through (the only sanctioned Redis-touching path,
  FLO-10 §6). This service owns no Redis client of its own.
* The **same** ``BROADCAST_SEGMENT`` channel-naming convention; a notification
  room is ``flock_os:notify:<axis>:<node>:broadcast`` — the per-org-tree-node
  analog of a gathering's broadcast room. **The org-tree node is the
  notification shard**: a 15k-member subtree fans out as one cheap publish per
  branch/group node, never one per recipient (the FLO-10 §5.1 sharding goal,
  re-applied on the membership axis instead of the crc32 attendee axis).
* The **same** ``RT_NOTIFICATION`` client-side websocket event name
  (``flock_os:notification``) the browser already listens for.
* Audience subtree resolution reuses :func:`flock_os.permissions.compute_branch_subtree`
  — the very primitive FLO-55's ``resolve_audience_branches`` delegates to — and
  the pure :func:`flock_os.flock_os.trees.subtree_of` for the group axis (DRY).

No cross-branch leakage — FLO-57 DoD *"no cross-branch leakage (Phase 1 perms
honored)"*:

* A **branch** scope resolves leaders ONLY within
  :func:`compute_branch_subtree(branch)` — the branch + its descendants. Sibling
  branches and the parent are never recipients and never published to.
* A **group** scope resolves leaders ONLY within the group's subtree, and the
  group must be branch-bound to a branch rooted in the scope's ``organization``
  (ADR §4 group-branch-binding + the tenant floor). Leaders outside the
  resolved subtree are never reached.

Transport-agnostic + import-clean without a Frappe site: the
:class:`NotificationFanoutGateway` port wraps the only Frappe calls; the
project-level pytest gate pins every rule against an in-memory gateway +
recording publisher (same hexagonal discipline as :mod:`flock_os.scheduling` /
:mod:`flock_os.realtime` / :mod:`flock_os.permissions`).

Wiring: the announcement DocType / admin controller (FLO-55 / FLO-8 §6) calls
:func:`fanout_scoped_notification` (or the service) on publish. That call site
lands with the announcement controller; this module is the reusable primitive
now, decoupled from its trigger.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from flock_os import events
from flock_os import permissions as perms
from flock_os.events import DomainEvent
from flock_os.flock_os import trees
from flock_os.flock_os.trees import ChildMap, ParentMap
from flock_os.realtime import (
	BROADCAST_SEGMENT,
	RT_NOTIFICATION,
	NullRealtimePublisher,
	RealtimePublisher,
)

# ---------------------------------------------------------------------------- #
# Scope-keyed notification channel naming — the FLO-14 broadcast convention
# re-applied on the org-tree-node axis (the Architect-owned contract extension).
# ---------------------------------------------------------------------------- #
NOTIFY_ROOM_PREFIX = "flock_os:notify"
BRANCH_AXIS = "branch"
GROUP_AXIS = "group"


def branch_notification_room(branch: str) -> str:
	"""The broadcast room every leader of a branch subscribes to for pushes.

	``flock_os:notify:branch:<branch>:broadcast`` — reuses FLO-14's
	``BROADCAST_SEGMENT``; the branch node is the notification shard.
	"""
	return f"{NOTIFY_ROOM_PREFIX}:{BRANCH_AXIS}:{branch}:{BROADCAST_SEGMENT}"


def group_notification_room(group: str) -> str:
	"""The broadcast room every leader of a group (subtree) subscribes to.

	``flock_os:notify:group:<group>:broadcast`` — the group node is the shard.
	"""
	return f"{NOTIFY_ROOM_PREFIX}:{GROUP_AXIS}:{group}:{BROADCAST_SEGMENT}"


#: Default audience for an admin push (FLO-57 title: "Admin → leader push fan-out").
#: The announcement's ``audience_role`` may broaden this later; FLO-57 ships the
#: leader fan-out primitive and forwards ``audience_role`` verbatim for the record.
DEFAULT_AUDIENCE_ROLE = "Leaders Only"


class FlockNotificationError(ValueError):
	"""Raised when a scoped notification's scope is invalid or violates a rule."""


# ---------------------------------------------------------------------------- #
# Scope — the row-level anchors of a push (the audience boundary).
# ---------------------------------------------------------------------------- #
@dataclass(frozen=True)
class NotificationScope:
	"""The org-tree boundary a push is confined to (FLO-8 §6.2 / ADR §6).

	Exactly one axis drives the fan-out: ``branch`` (branch + its subtree) or
	``group`` (the group's subtree). When both are set the group must be
	branch-bound to ``branch`` (ADR §4). ``organization`` is the tenant floor.
	"""

	organization: str
	branch: str | None = None
	group: str | None = None

	@property
	def axis(self) -> str:
		"""Which tree axis the fan-out walks (``group`` wins when both are set)."""
		if self.group:
			return GROUP_AXIS
		if self.branch:
			return BRANCH_AXIS
		return ""


# ---------------------------------------------------------------------------- #
# NotificationFanoutGateway port (hexagonal) — the only Frappe-touching surface.
#
# Production: FrappeNotificationFanoutGateway (lazy Frappe import). Unit tests:
# RecordingNotificationFanoutGateway (in flock_os.tests.test_notifications).
# Returns plain data so the resolver + service stay Frappe-agnostic.
# ---------------------------------------------------------------------------- #
@runtime_checkable
class NotificationFanoutGateway(Protocol):
	"""Port: the tree-membership + leader-resolution reads the fan-out needs."""

	def branch_exists(self, branch: str, organization: str) -> bool:
		"""True iff ``branch`` exists and is rooted under ``organization``."""
		...

	def group_exists(self, group: str) -> bool:
		"""True iff ``group`` exists (regardless of branch)."""
		...

	def group_branch(self, group: str) -> str | None:
		"""The ``Flock Group.branch`` for ``group`` (ADR §4 branch-binding), or ``None``."""
		...

	def branch_parent_of(self) -> ParentMap:
		"""Full branch-tree adjacency (root → leaves) for subtree resolution."""
		...

	def branch_children_of(self) -> ChildMap:
		"""Full branch-tree child map mirroring :meth:`branch_parent_of`."""
		...

	def group_children_of(self) -> ChildMap:
		"""Full group-tree child map for group-subtree resolution."""
		...

	def leaders_in_branches(self, branches: Sequence[str]) -> tuple[str, ...]:
		"""Distinct leader member refs of every group rooted in ``branches``.

		Leadership = ``Flock Group.leader`` ∪ ``Flock Group Member(role ∈
		Leader/Co-Leader)`` (ADR §4.3 — the same union the permission spine's
		``fetch_led_group_bounds`` resolves). De-duplicated, order-stable.
		"""
		...

	def leaders_in_groups(self, groups: Sequence[str]) -> tuple[str, ...]:
		"""Distinct leader member refs of every group in ``groups`` (subtree set)."""
		...


class NullNotificationFanoutGateway:
	"""Empty gateway — yields no tree knowledge + no leaders (default before wiring)."""

	def branch_exists(self, branch: str, organization: str) -> bool:  # noqa: ARG002
		return False

	def group_exists(self, group: str) -> bool:  # noqa: ARG002
		return False

	def group_branch(self, group: str) -> str | None:  # noqa: ARG002
		return None

	def branch_parent_of(self) -> ParentMap:
		return {}

	def branch_children_of(self) -> ChildMap:
		return {}

	def group_children_of(self) -> ChildMap:
		return {}

	def leaders_in_branches(self, branches: Sequence[str]) -> tuple[str, ...]:  # noqa: ARG002
		return ()

	def leaders_in_groups(self, groups: Sequence[str]) -> tuple[str, ...]:  # noqa: ARG002
		return ()


# ---------------------------------------------------------------------------- #
# Pure audience resolver — validates the scope + resolves the recipient set.
#
# No I/O: the gateway has already returned plain adjacency + leader data. The
# subtree walk delegates to compute_branch_subtree / trees.subtree_of (DRY — no
# parallel traversal). This is the function the "no cross-branch leakage" DoD
# pins: the recipient set is, by construction, confined to the scope's subtree.
# ---------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ScopedAudience:
	"""The resolved recipient view of a scoped push (FLO-57)."""

	axis: str
	"""``branch`` or ``group`` — which tree axis the fan-out walked."""

	nodes: tuple[str, ...]
	"""The subtree nodes published to (one shard room per node)."""

	leaders: tuple[str, ...]
	"""Distinct leader member refs — the actual recipient set (the audience)."""

	@property
	def size(self) -> int:
		return len(self.leaders)


def _validate_scope(*, scope: NotificationScope, gateway: NotificationFanoutGateway) -> None:
	# Tenant floor + at least one axis anchor (ADR §6.1 / FLO-8 §3).
	if not scope.organization:
		raise FlockNotificationError("Notification organization is required (tenant floor).")
	if not scope.branch and not scope.group:
		raise FlockNotificationError("Notification scope requires a branch or group anchor.")
	if scope.group:
		if not gateway.group_exists(scope.group):
			raise FlockNotificationError(f"Group {scope.group!r} does not exist.")
		group_branch = gateway.group_branch(scope.group)
		if not group_branch:
			raise FlockNotificationError(f"Group {scope.group!r} has no branch binding (ADR §4).")
		# Tenant floor: the group's branch must be rooted in the scope's org.
		if not gateway.branch_exists(group_branch, scope.organization):
			raise FlockNotificationError(
				f"Group {scope.group!r}'s branch {group_branch!r} is not a member of "
				f"organization {scope.organization!r}."
			)
		# ADR §4 group-branch-binding: an explicit branch anchor must agree with
		# the group's own branch (a push scoped to North may not target G-South).
		if scope.branch and scope.branch != group_branch:
			raise FlockNotificationError(
				f"Group {scope.group!r} belongs to branch {group_branch!r}, not the scope's "
				f"branch {scope.branch!r} (group-branch-binding, ADR §4)."
			)
	elif scope.branch:
		if not gateway.branch_exists(scope.branch, scope.organization):
			raise FlockNotificationError(
				f"Branch {scope.branch!r} is not a member of organization {scope.organization!r}."
			)


def resolve_scoped_audience(
	*, scope: NotificationScope, gateway: NotificationFanoutGateway
) -> ScopedAudience:
	"""Resolve the leader recipient set for ``scope`` (FLO-57 DoD: no leakage).

	Pure orchestrator. Branch axis → :func:`compute_branch_subtree` (the DRY
	primitive); group axis → :func:`trees.subtree_of`. Leaders come from the
	gateway, scoped to exactly the resolved subtree nodes — never siblings.
	"""
	_validate_scope(scope=scope, gateway=gateway)
	if scope.group:
		children_of = gateway.group_children_of()
		nodes = tuple(trees.subtree_of(scope.group, children_of))
		leaders = gateway.leaders_in_groups(nodes)
		return ScopedAudience(axis=GROUP_AXIS, nodes=nodes, leaders=leaders)
	# branch axis (scope.branch is guaranteed by _validate_scope when group is absent).
	assert scope.branch is not None  # noqa: S101 - narrowed by _validate_scope
	nodes = perms.compute_branch_subtree(
		scope.branch,
		parent_of=gateway.branch_parent_of(),
		children_of=gateway.branch_children_of(),
	)
	leaders = gateway.leaders_in_branches(nodes)
	return ScopedAudience(axis=BRANCH_AXIS, nodes=nodes, leaders=leaders)


def rooms_for(audience: ScopedAudience) -> tuple[str, ...]:
	"""The scoped shard rooms a fan-out publishes to (one per subtree node).

	Branch axis → :func:`branch_notification_room`; group axis →
	:func:`group_notification_room`. The node set is the audience subtree, so the
	rooms reach exactly the resolved leaders (no cross-branch leakage by design).
	"""
	if audience.axis == GROUP_AXIS:
		return tuple(group_notification_room(n) for n in audience.nodes)
	return tuple(branch_notification_room(n) for n in audience.nodes)


# ---------------------------------------------------------------------------- #
# Fan-out result — the audit/test surface for one dispatched push.
# ---------------------------------------------------------------------------- #
@dataclass(frozen=True)
class NotificationFanoutResult:
	"""The outcome of one scoped push fan-out (FLO-57)."""

	scope: NotificationScope
	audience: ScopedAudience
	rooms: tuple[str, ...]
	event: DomainEvent
	"""The canonical ``flock.notification.sent`` event that was emitted."""

	audience_role: str = DEFAULT_AUDIENCE_ROLE
	subject: str = ""
	body: str = ""
	category: str | None = None
	notification_ref: str | None = field(default=None)
	"""Optional id of the originating ``Flock Announcement`` / notification doc."""

	@property
	def recipient_count(self) -> int:
		return self.audience.size


# ---------------------------------------------------------------------------- #
# The service — composes gateway + publisher + canonical emitter.
# ---------------------------------------------------------------------------- #
class ScopedNotificationService:
	"""Admin → leader scoped push fan-out (FLO-57).

	Resolves the audience, fans the push to the scoped shard rooms via the FLO-14
	:class:`RealtimePublisher` port (best-effort — realtime owns UX, not
	correctness, FLO-10 §5.3), and emits one ``flock.notification.sent`` through
	the canonical :mod:`flock_os.events` emitter (audit/outbox + server-side
	subscribers). The emit is ``realtime=False`` because the browser push is the
	direct ``RT_NOTIFICATION`` publish; the canonical event is the durable record.
	"""

	def __init__(
		self,
		gateway: NotificationFanoutGateway,
		publisher: RealtimePublisher | None = None,
		bus: Any = events,
	) -> None:
		self._gw = gateway
		self._publisher: RealtimePublisher = publisher or NullRealtimePublisher()
		self._bus = bus

	def install_publisher(self, publisher: RealtimePublisher) -> None:
		"""Swap the realtime publisher (production wiring / tests)."""
		self._publisher = publisher

	def fanout(
		self,
		*,
		scope: NotificationScope,
		subject: str,
		body: str = "",
		audience_role: str = DEFAULT_AUDIENCE_ROLE,
		category: str | None = None,
		notification_ref: str | None = None,
	) -> NotificationFanoutResult:
		"""Dispatch one scoped push (resolve → publish → emit) and return the result.

		Raises :class:`FlockNotificationError` if the scope is invalid. An empty
		audience (no leaders in the subtree) is a valid no-op publish: the event
		is still emitted (audit) but no realtime room is touched.
		"""
		audience = resolve_scoped_audience(scope=scope, gateway=self._gw)
		# No recipients anywhere in the subtree → nothing to deliver realtime;
		# the canonical event is still emitted below for the audit record.
		rooms = rooms_for(audience) if audience.size > 0 else ()
		message: dict[str, Any] = {
			"subject": subject,
			"body": body,
			"audience_role": audience_role,
			"axis": audience.axis,
			"nodes": list(audience.nodes),
			"audience_size": audience.size,
			"organization": scope.organization,
			"branch": scope.branch,
			"group": scope.group,
		}
		if category is not None:
			message["category"] = category
		if notification_ref is not None:
			message["notification_ref"] = notification_ref
		# FLO-14 reuse: the sanctioned RealtimePublisher port (FLO-10 §6). Best-
		# effort — a publish failure never fails the originating request (§5.3).
		for room in rooms:
			try:
				self._publisher.publish(event=RT_NOTIFICATION, room=room, message=dict(message))
			except Exception:  # noqa: BLE001 - realtime is best-effort (FLO-10 §5.3)
				pass
		# Canonical emitter — the single sanctioned event-publish path (ADR §5.1).
		# realtime=False: the browser push is the RT_NOTIFICATION publish above;
		# this emit is the durable record (outbox) + in-process subscriber dispatch.
		event = self._bus.emit(
			events.NOTIFICATION_SENT,
			payload={
				"subject": subject,
				"body": body,
				"audience_role": audience_role,
				"axis": audience.axis,
				"nodes": list(audience.nodes),
				"audience_size": audience.size,
				"category": category,
				"notification_ref": notification_ref,
			},
			scope={"organization": scope.organization, "branch": scope.branch, "group": scope.group},
			realtime=False,
		)
		return NotificationFanoutResult(
			scope=scope,
			audience=audience,
			rooms=rooms,
			event=event,
			audience_role=audience_role,
			subject=subject,
			body=body,
			category=category,
			notification_ref=notification_ref,
		)


def fanout_scoped_notification(
	*,
	scope: NotificationScope,
	subject: str,
	gateway: NotificationFanoutGateway,
	publisher: RealtimePublisher | None = None,
	bus: Any = events,
	body: str = "",
	audience_role: str = DEFAULT_AUDIENCE_ROLE,
	category: str | None = None,
	notification_ref: str | None = None,
) -> NotificationFanoutResult:
	"""One-shot scoped push fan-out (the unit-test entry point).

	Equivalent to building a :class:`ScopedNotificationService` and calling
	:meth:`fanout`. Production wires the service once (``get_service``) and reuses
	it; tests call this directly with an in-memory gateway + recording publisher.
	"""
	service = ScopedNotificationService(gateway, publisher, bus)
	return service.fanout(
		scope=scope,
		subject=subject,
		body=body,
		audience_role=audience_role,
		category=category,
		notification_ref=notification_ref,
	)


# ---------------------------------------------------------------------------- #
# Frappe adapter (lazy import — this module stays import-clean under pytest).
# ---------------------------------------------------------------------------- #
class FrappeNotificationFanoutGateway:
	"""Production adapter: tree-membership + leader reads over ``frappe.get_all``.

	Leadership mirrors :class:`flock_os.permissions.FrappePermissionGateway.fetch_led_group_bounds`
	(ADR §4.3): ``Flock Group.leader`` ∪ ``Flock Group Member(role ∈
	Leader/Co-Leader)``, de-duplicated. Reads go through ``frappe.get_all`` so the
	caller's role + User Permissions apply automatically (ADR §6.2 branch axis).
	"""

	@property
	def _frappe(self):  # type: ignore[no-untyped-def]
		import frappe

		return frappe

	def branch_exists(self, branch: str, organization: str) -> bool:
		frappe = self._frappe
		row = frappe.db.get_value("Flock Branch", branch, "organization")
		return bool(row) and row == organization

	def group_exists(self, group: str) -> bool:
		frappe = self._frappe
		return bool(frappe.db.exists("Flock Group", group))

	def group_branch(self, group: str) -> str | None:
		frappe = self._frappe
		return frappe.db.get_value("Flock Group", group, "branch")

	def branch_parent_of(self) -> ParentMap:
		frappe = self._frappe
		rows = frappe.get_all("Flock Branch", fields=["name", "parent_branch"], ignore_permissions=True)
		return {r["name"]: r.get("parent_branch") or None for r in rows}

	def branch_children_of(self) -> ChildMap:
		parent_of = self.branch_parent_of()
		children: ChildMap = {name: [] for name in parent_of}
		for name, parent in parent_of.items():
			if parent and parent in children:
				children[parent].append(name)
		return children

	def group_children_of(self) -> ChildMap:
		frappe = self._frappe
		rows = frappe.get_all("Flock Group", fields=["name", "parent_group"], ignore_permissions=True)
		parent_of = {r["name"]: r.get("parent_group") or None for r in rows}
		children: ChildMap = {name: [] for name in parent_of}
		for name, parent in parent_of.items():
			if parent and parent in children:
				children[parent].append(name)
		return children

	def leaders_in_branches(self, branches: Sequence[str]) -> tuple[str, ...]:
		return self._leaders(filters_branch=list(branches) if branches else [])

	def leaders_in_groups(self, groups: Sequence[str]) -> tuple[str, ...]:
		return self._leaders(filters_group=list(groups) if groups else [])

	def _leaders(
		self, *, filters_branch: list[str] | None = None, filters_group: list[str] | None = None
	) -> tuple[str, ...]:
		frappe = self._frappe
		filters_branch = filters_branch or []
		leader_roles = list(perms.LEADER_ROSTER_ROLES)
		refs: list[str] = []
		# Accountable leaders: Flock Group.leader, scoped by branch or group.
		group_filters: dict[str, Any] = {}
		if filters_branch:
			group_filters["branch"] = ["in", filters_branch]
		if filters_group:
			group_filters["name"] = ["in", filters_group]
		if group_filters:
			refs.extend(r for r in frappe.get_all("Flock Group", filters=group_filters, pluck="leader") if r)
		# Roster leaders: Flock Group Member(role ∈ Leader/Co-Leader, Active).
		# `status = 'Active'` is required so Inactive leaders do not receive
		# notifications (FLO-465 §1 — latent correctness bug surfaced by the
		# FLO-459 review). The predicate also aligns the read with the
		# `(branch, status)` / `(group, status)` composites (FLO-459) so the
		# fan-out path is index-served.
		roster_filters: dict[str, Any] = {"role": ["in", leader_roles], "status": "Active"}
		if filters_branch:
			roster_filters["branch"] = ["in", filters_branch]
		if filters_group:
			roster_filters["group"] = ["in", filters_group]
		refs.extend(
			r for r in frappe.get_all("Flock Group Member", filters=roster_filters, pluck="member") if r
		)
		# De-duplicate, preserve order.
		seen: dict[str, None] = dict.fromkeys(refs)
		return tuple(seen)


# ---------------------------------------------------------------------------- #
# Module-level service accessor (lazy; production wires the Frappe gateway).
# ---------------------------------------------------------------------------- #
_service: ScopedNotificationService | None = None


def get_service() -> ScopedNotificationService:
	"""Process-wide scoped-notification service (lazily built, singleton per process)."""
	global _service
	if _service is None:
		_service = ScopedNotificationService(FrappeNotificationFanoutGateway())
	return _service


def install_gateway(gateway: NotificationFanoutGateway) -> ScopedNotificationService:
	"""Install a custom gateway (production wiring / tests) and return the service."""
	global _service
	_service = ScopedNotificationService(gateway)
	return _service


def install_publisher(publisher: RealtimePublisher) -> None:
	"""Install the production/test publisher on the module-level service."""
	get_service().install_publisher(publisher)
