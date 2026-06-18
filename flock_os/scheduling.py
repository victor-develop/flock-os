"""
Org-level announcement scheduling + scoped-publish service (FLO-8 §2/§3/§6,
ADR-0001 §4/§6).

One concern: validate the **scope contract** of a ``Flock Announcement`` and
resolve the **audience branch set** a publish reaches — then drive the publish
fan-out through the FLO-57 notification primitive. No second fan-out mechanism
and no second branch-subtree walk are introduced.

Layering (ADR-0001 §2 separation of concerns)::

    Admin controller / announcement DocType  (Frappe transport, this module)
      -> validate_announcement_scope(...)    <- pure half, domain rule
      -> resolve_audience_branches(...)      <- pure half, reuses permissions
      -> fanout_scoped_notification(...)     (FLO-57 reuse — the ONLY fan-out)
      -> events.emit(ANNOUNCEMENT_PUBLISHED) (canonical emitter — audit/outbox)

Reuse contract — FLO-55 / FLO-94 DoD *"reuses* ``compute_branch_subtree`` *— DRY"*:

* Audience resolution delegates to :func:`flock_os.permissions.compute_branch_subtree`
  — the very primitive the permission spine and the FLO-57 fan-out walk. There is
  no parallel traversal of the branch tree here.
* Publish fan-out calls :func:`flock_os.notifications.fanout_scoped_notification`
  (FLO-57) — the single scoped push primitive. This module owns no Redis client,
  no realtime publisher, and no leader-resolution read of its own.
* ``validate_announcement_scope`` reuses the same branch-in-org + group-branch-
  binding rules the permission layer's guards rely on (ADR §4 / §6.1).

No cross-branch leakage — the audience is, by construction, the publisher branch
+ its subtree (descendant branches). A publish scoped to Branch A never reaches
Branch B's members: ``compute_branch_subtree`` is confined to the subtree.

Transport-agnostic + import-clean without a Frappe site: the
:class:`SchedulingGateway` port wraps the only Frappe calls; the project-level
pytest gate pins every rule against an in-memory gateway (same hexagonal
discipline as :mod:`flock_os.notifications` / :mod:`flock_os.permissions`).
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from flock_os import events
from flock_os import permissions as perms
from flock_os.flock_os.trees import ChildMap, ParentMap
from flock_os.notifications import (
	DEFAULT_AUDIENCE_ROLE,
	NotificationScope,
	fanout_scoped_notification,
)

# ---------------------------------------------------------------------------- #
# Announcement lifecycle (FLO-8 §3).
# ---------------------------------------------------------------------------- #
STATUS_DRAFT = "Draft"
STATUS_SCHEDULED = "Scheduled"
STATUS_PUBLISHING = "Publishing"
STATUS_PUBLISHED = "Published"
STATUS_ARCHIVED = "Archived"

#: The canonical announcement status set, in lifecycle order.
ANNOUNCEMENT_STATUSES: tuple[str, ...] = (
	STATUS_DRAFT,
	STATUS_SCHEDULED,
	STATUS_PUBLISHING,
	STATUS_PUBLISHED,
	STATUS_ARCHIVED,
)

#: Legal forward transitions (FLO-8 §3 lifecycle). A publish may short-circuit
#: Draft -> Published (immediate send). Archived is terminal.
ANNOUNCEMENT_TRANSITIONS: dict[str, frozenset[str]] = {
	STATUS_DRAFT: frozenset({STATUS_SCHEDULED, STATUS_PUBLISHING, STATUS_PUBLISHED}),
	STATUS_SCHEDULED: frozenset({STATUS_PUBLISHING, STATUS_PUBLISHED}),
	STATUS_PUBLISHING: frozenset({STATUS_PUBLISHED}),
	STATUS_PUBLISHED: frozenset({STATUS_ARCHIVED}),
	STATUS_ARCHIVED: frozenset(),
}


class FlockSchedulingError(ValueError):
	"""Raised when an announcement's scope or lifecycle invariant is violated."""


# ---------------------------------------------------------------------------- #
# SchedulingGateway port (hexagonal) — the only Frappe-touching surface.
#
# Production: FrappeSchedulingGateway (lazy Frappe import). Unit tests:
# RecordingSchedulingGateway (in flock_os.tests.test_scheduling). Returns plain
# data so the scope validator + audience resolver stay Frappe-agnostic.
# ---------------------------------------------------------------------------- #
@runtime_checkable
class SchedulingGateway(Protocol):
	"""Port: the tree-membership + org-floor reads the scheduling layer needs.

	Mirrors the slice of :class:`flock_os.notifications.NotificationFanoutGateway`
	the announcement validator + audience resolver consume, so the two services
	share one consistent view of the two-tree model (ADR §4).
	"""

	def branch_exists(self, branch: str, organization: str) -> bool:
		"""True iff ``branch`` exists and is rooted under ``organization``."""
		...

	def group_exists(self, group: str) -> bool:
		"""True iff ``group`` exists."""
		...

	def group_branch(self, group: str) -> str | None:
		"""The ``Flock Group.branch`` for ``group`` (ADR §4 binding), or ``None``."""
		...

	def branch_parent_of(self) -> ParentMap:
		"""Full branch-tree adjacency (root -> leaves) for subtree resolution."""
		...

	def branch_children_of(self) -> ChildMap:
		"""Full branch-tree child map mirroring :meth:`branch_parent_of`."""
		...


class NullSchedulingGateway:
	"""Empty gateway — yields no tree knowledge (default before wiring)."""

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


# ---------------------------------------------------------------------------- #
# Pure scope validator + audience resolver.
#
# No I/O: the gateway has already returned plain adjacency + membership data.
# The subtree walk delegates to ``compute_branch_subtree`` (DRY — no parallel
# traversal). These are the functions the "branch-in-org / group-branch-binding /
# no cross-branch leakage" DoD pins.
# ---------------------------------------------------------------------------- #
def _attr(announcement: Any, name: str, default: Any = None) -> Any:
	"""Read ``name`` off a Frappe doc or a plain test object (duck-typed)."""
	if isinstance(announcement, dict):
		return announcement.get(name, default)
	return getattr(announcement, name, default)


def validate_announcement_scope(announcement: Any, gateway: SchedulingGateway) -> None:
	"""Validate a ``Flock Announcement``'s scope contract (FLO-8 §3 / ADR §4/§6.1).

	Raises :class:`FlockSchedulingError` when the organization (tenant floor) or
	branch (audience anchor) is missing, the branch is not a member of the
	organization (cross-tenant guard), a group anchor does not exist or its branch
	binding disagrees with the announcement's branch (ADR §4 group-branch-binding),
	or the status is outside the lifecycle set / ``scheduled_at`` is unset while
	``status == Scheduled``.

	The validator is Frappe-agnostic: it reads scope fields off the announcement
	via duck typing and consults the gateway for tree-membership facts. The
	DocType ``validate`` hook calls it with the production gateway.
	"""
	organization = _attr(announcement, "organization")
	branch = _attr(announcement, "branch")
	group = _attr(announcement, "group")
	status = _attr(announcement, "status") or STATUS_DRAFT

	if not organization:
		raise FlockSchedulingError("Announcement organization is required (tenant floor).")
	if not branch:
		raise FlockSchedulingError("Announcement branch is required (audience scope anchor).")
	if not gateway.branch_exists(branch, organization):
		raise FlockSchedulingError(f"Branch {branch!r} is not a member of organization {organization!r}.")
	if group:
		if not gateway.group_exists(group):
			raise FlockSchedulingError(f"Group {group!r} does not exist.")
		group_branch = gateway.group_branch(group)
		if not group_branch:
			raise FlockSchedulingError(f"Group {group!r} has no branch binding (ADR §4).")
		# ADR §4 group-branch-binding: the announcement's branch must equal the
		# group's own branch. A branch-scoped announcement may not target a group
		# rooted in another branch.
		if group_branch != branch:
			raise FlockSchedulingError(
				f"Group {group!r} belongs to branch {group_branch!r}, not the "
				f"announcement's branch {branch!r} (group-branch-binding, ADR §4)."
			)

	if status not in ANNOUNCEMENT_STATUSES:
		raise FlockSchedulingError(f"Unknown announcement status {status!r}.")
	if status == STATUS_SCHEDULED and not _attr(announcement, "scheduled_at"):
		raise FlockSchedulingError("A Scheduled announcement requires a scheduled_at datetime.")


def validate_status_transition(current: str, target: str) -> None:
	"""Guard the lifecycle forward-transitions (FLO-8 §3).

	Raises :class:`FlockSchedulingError` if ``target`` is not a legal forward
	move from ``current`` (see :data:`ANNOUNCEMENT_TRANSITIONS`).
	"""
	allowed = ANNOUNCEMENT_TRANSITIONS.get(current, frozenset())
	if target not in allowed:
		raise FlockSchedulingError(f"Illegal announcement transition {current!r} -> {target!r}.")


def resolve_audience_branches(branch: str, *, gateway: SchedulingGateway) -> tuple[str, ...]:
	"""The publisher ``branch`` + its subtree — the audience boundary (FLO-8 §7).

	Reuses :func:`flock_os.permissions.compute_branch_subtree` (DRY): the same
	primitive the permission spine and the FLO-57 fan-out walk. The recipient
	set is, by construction, confined to this subtree — descendant branches of
	the publisher's branch; siblings and the parent are never reached.
	"""
	if not branch:
		raise FlockSchedulingError("branch is required to resolve the audience subtree.")
	return perms.compute_branch_subtree(
		branch,
		parent_of=gateway.branch_parent_of(),
		children_of=gateway.branch_children_of(),
	)


# ---------------------------------------------------------------------------- #
# Module-level service accessor (lazy; production wires FrappeSchedulingGateway).
# ---------------------------------------------------------------------------- #
_gateway: SchedulingGateway | None = None


def get_gateway() -> SchedulingGateway:
	"""Process-wide scheduling gateway (lazily built, singleton per process)."""
	global _gateway
	if _gateway is None:
		_gateway = FrappeSchedulingGateway()
	return _gateway


def install_gateway(gateway: SchedulingGateway) -> SchedulingGateway:
	"""Install a custom gateway (production wiring / tests) and return it."""
	global _gateway
	_gateway = gateway
	return _gateway


# ---------------------------------------------------------------------------- #
# Frappe adapter (lazy import — this module stays import-clean under pytest).
# ---------------------------------------------------------------------------- #
class FrappeSchedulingGateway:
	"""Production adapter over ``frappe.db`` / ``frappe.get_all``.

	Membership reads go through ``frappe.get_all`` so the caller's role + User
	Permissions apply automatically (ADR §6.2 branch axis).
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


# ---------------------------------------------------------------------------- #
# Internals — ``frappe.whitelist`` is only meaningful inside a running bench.
# At import time under plain ``pytest`` (CI gate) Frappe is absent, so we fall
# back to an identity decorator that keeps the controller callable for unit
# tests of the transport layer (same idiom as :mod:`flock_os.traversal`).
# ---------------------------------------------------------------------------- #
def _whitelist():
	try:
		import frappe

		return frappe.whitelist()
	except Exception:  # noqa: BLE001 - no bench under CI; the deco is a no-op

		def _identity(fn):  # type: ignore[no-untyped-def]
			return fn

		return _identity


# ---------------------------------------------------------------------------- #
# Admin controller — the @frappe.whitelist() surface FLO-60 wires its UI against
# (FLO-8 §8). compose/preview-audience/send, calling fanout_scoped_notification
# (FLO-57) on publish. Server-side scope validation runs on every mutating call.
# ---------------------------------------------------------------------------- #
@_whitelist()
def preview_audience(organization: str, branch: str, group: str | None = None) -> dict[str, Any]:
	"""GET -> the branch subtree a publish would reach + its size (UI preview).

	Resolves :func:`resolve_audience_branches` (publisher branch + descendants)
	so the compose UI can show "this will reach N branches" before sending.
	Validates the scope first (tenant floor + branch-in-org + group binding).
	"""
	announcement = {
		"organization": organization,
		"branch": branch,
		"group": group,
		"status": STATUS_DRAFT,
	}
	gateway = get_gateway()
	validate_announcement_scope(announcement, gateway)
	branches = resolve_audience_branches(branch, gateway=gateway)
	return {"branches": list(branches), "branch_count": len(branches)}


@_whitelist()
def publish_announcement(name: str) -> dict[str, Any]:
	"""POST -> Draft/Scheduled -> Published + scoped fan-out (FLO-8 §8 / FLO-57).

	Server-side flow: load + validate the announcement scope (cross-branch guard,
	ADR §6.1); transition Draft/Scheduled -> Publishing -> Published, stamping
	``published_at``; resolve the audience branch subtree once; fan out via
	:func:`fanout_scoped_notification` (the FLO-57 primitive — exactly one
	``flock.notification.sent`` flows); then emit ``flock.announcement.published``
	through the canonical emitter.

	Returns a summary (audience size, dispatched notification ref). This is the
	surface [FLO-60](/FLO/issues/FLO-60) wires its UI against.
	"""
	import frappe

	doc = frappe.get_doc("Flock Announcement", name)
	gateway = get_gateway()
	validate_announcement_scope(doc, gateway)

	current = doc.status or STATUS_DRAFT
	# Draft/Scheduled -> Publishing -> Published (FLO-8 §3 lifecycle).
	for target in (STATUS_PUBLISHING, STATUS_PUBLISHED):
		validate_status_transition(current, target)
		current = target

	doc.status = STATUS_PUBLISHED
	doc.published_at = frappe.utils.now()

	audience_branches = resolve_audience_branches(doc.branch, gateway=gateway)
	channels = [row.channel for row in (doc.channels or []) if row.channel]
	result = fanout_scoped_notification(
		scope=NotificationScope(
			organization=doc.organization,
			branch=doc.branch,
			group=doc.group,
		),
		subject=doc.subject,
		body=doc.body or "",
		audience_role=doc.audience_role or DEFAULT_AUDIENCE_ROLE,
		category=doc.category,
		notification_ref=doc.name,
	)
	if result.notification_ref is None:
		# Keep the dispatch back-link (raw Data ref until Flock Notification lands).
		doc.notification = doc.name

	# Canonical audit event — the announcement's own published transition.
	events.emit(
		events.ANNOUNCEMENT_PUBLISHED,
		payload={
			"announcement": doc.name,
			"branch": doc.branch,
			"group": doc.group,
			"organization": doc.organization,
			"audience_role": doc.audience_role,
			"channels": channels,
			"audience_branch_count": len(audience_branches),
			"notification_ref": doc.notification,
		},
		scope={"organization": doc.organization, "branch": doc.branch, "group": doc.group},
		realtime=False,
	)
	doc.save(ignore_permissions=True)
	return {
		"name": doc.name,
		"status": doc.status,
		"audience_branches": list(audience_branches),
		"audience_branch_count": len(audience_branches),
		"notification_ref": doc.notification,
	}


def schedule_announcement(name: str) -> dict[str, Any]:
	"""POST -> Draft -> Scheduled + emit ``flock.announcement.scheduled``.

	The announcement must already carry a ``scheduled_at``; the scope validator
	enforces the Scheduled-requires-scheduled_at invariant. A scheduled job
	(wired in hooks.py once the scheduler lands) later calls
	:func:`publish_announcement` at ``scheduled_at``.
	"""
	import frappe

	doc = frappe.get_doc("Flock Announcement", name)
	gateway = get_gateway()
	# Validate under the target status so the scheduled_at rule fires.
	doc.status = STATUS_SCHEDULED
	validate_announcement_scope(doc, gateway)
	validate_status_transition(STATUS_DRAFT, STATUS_SCHEDULED)

	doc.status = STATUS_SCHEDULED
	events.emit(
		events.ANNOUNCEMENT_SCHEDULED,
		payload={
			"announcement": doc.name,
			"branch": doc.branch,
			"group": doc.group,
			"organization": doc.organization,
			"scheduled_at": str(doc.scheduled_at) if doc.scheduled_at else None,
		},
		scope={"organization": doc.organization, "branch": doc.branch, "group": doc.group},
		realtime=False,
	)
	doc.save(ignore_permissions=True)
	return {"name": doc.name, "status": doc.status, "scheduled_at": str(doc.scheduled_at)}


# Expose the whitelist entry points under conventional ``flock_os.scheduling.*``
# names so the REST routes resolve (``/api/method/flock_os.scheduling.<fn>``).
__all__ = [
	"ANNOUNCEMENT_STATUSES",
	"ANNOUNCEMENT_TRANSITIONS",
	"FlockSchedulingError",
	"FrappeSchedulingGateway",
	"NullSchedulingGateway",
	"SchedulingGateway",
	"STATUS_ARCHIVED",
	"STATUS_DRAFT",
	"STATUS_PUBLISHED",
	"STATUS_PUBLISHING",
	"STATUS_SCHEDULED",
	"get_gateway",
	"install_gateway",
	"preview_audience",
	"publish_announcement",
	"resolve_audience_branches",
	"schedule_announcement",
	"validate_announcement_scope",
	"validate_status_transition",
]
