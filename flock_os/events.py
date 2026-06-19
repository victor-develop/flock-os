"""
Domain event emitter + in-process subscriber bus (FLO-14).

This is the **single sanctioned event-publish path** for Flock OS
(ADR-0001 §5.1, [FLO-4](/FLO/issues/FLO-4)). Every domain state change flows
through :func:`emit`; no feature code calls ``frappe.publish_realtime`` or a
raw Redis client directly for domain events (ADR-0001 §2, FLO-10 §6).

Layering (ADR-0001 §5, FLO-10 §5.2)::

    Frappe doc-event hook (hooks.py)
      -> flock_os.events.on_doc_event(...)
      -> emit(name, payload, scope)            <- THIS module, single publisher
          |-> in-process subscriber dispatch   <- the realtime projector (FLO-14),
          |                                     rollup subscribers, etc. register here
          |-> sink.publish(event)              <- port: Redis pub/sub + Frappe
                                                  realtime + Flock Event Outbox

The module is **transport-agnostic and import-clean without a Frappe site**:
:func:`emit` / :func:`subscribe` operate on a module-level :class:`EventBus`
whose side-effecting :class:`EventSink` port defaults to :class:`FrappeEventSink`
(lazy Frappe import) in production and to :class:`RecordingEventSink` in the
unit suite. The realtime fan-out projector (:mod:`flock_os.realtime`) registers
itself here, so ``emit("flock.attendance.bulk_recorded", ...)`` fans out to the
sharded event-room channels end-to-end (FLO-14 DoD #1/#2).

Design rules honoured:

* **All Redis access via Frappe abstractions only** — ``frappe.publish_realtime``
  + the outbox DocType. No bespoke Redis clients (keeps the D3 cluster escape
  hatch from FLO-10 §6 viable).
* **Reporter ack cheap + synchronous; room updates best-effort** (FLO-10 §5.3).
  Correctness stays in the queue (FLO-15); realtime owns UX.
* **At-least-once delivery** — every emit is appended to the ``Flock Event
  Outbox`` for audit + replay; subscribers must be idempotent (ADR-0001 §5.1).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

# ---------------------------------------------------------------------------- #
# Domain event catalog (ADR-0001 §5.3 / §5.4). The canonical names every feature
# emits/subscribes to. Aggregate = the DocType/concept; verb = past-tense state
# change. This is the single source of truth for event names.
# ---------------------------------------------------------------------------- #
BRANCH_CREATED = "flock.branch.created"
BRANCH_MOVED = "flock.branch.moved"
GROUP_CREATED = "flock.group.created"
GROUP_MOVED = "flock.group.moved"
GROUP_MEMBER_ADDED = "flock.group_member.added"
MEMBER_CREATED = "flock.member.created"
GATHERING_CREATED = "flock.gathering.created"
GATHERING_SUBMITTED = "flock.gathering.submitted"
GATHERING_APPROVED = "flock.gathering.approved"
GATHERING_CANCELLED = "flock.gathering.cancelled"
ATTENDANCE_RECORDED = "flock.attendance.recorded"
ATTENDANCE_REPORTED = "flock.attendance.reported"
"""Leader attendance-report workflow transition (FLO-6 §4 / [FLO-56](/FLO/issues/FLO-56)).

Emitted once per *report submission* — when a group leader submits the
attendance report for a gathering (members + visitors / pre-members recorded
together), driving the gathering ``Held → Reported`` transition. Distinct from
the per-row :data:`ATTENDANCE_RECORDED` and per-batch
:data:`ATTENDANCE_BULK_RECORDED`: those describe individual attendance writes;
this describes the aggregate *report* act the leader confirms. Routed through
the single sanctioned :func:`emit` (no dual emitters, ADR-0001 §5.1)."""
ATTENDANCE_BULK_RECORDED = "flock.attendance.bulk_recorded"
ATTENDANCE_BATCH_REJECTED = "flock.attendance.batch_rejected"
ATTENDANCE_IMPORT_FAILED = "flock.attendance.import_failed"
"""Dead-letter transition: a batch exhausted retries and was parked on the
visible ``attendance_import_error`` queue (FLO-10 §3.3). Emitted by the
reporting dead-letter path ([FLO-15](/FLO/issues/FLO-15)); the catalog — not a
local constant — is its single source of truth (ADR-0001 §5.3/§5.4)."""
ENGAGEMENT_SESSION_OPENED = "flock.engagement.session.opened"
ENGAGEMENT_SESSION_CLOSED = "flock.engagement.session.closed"
ANNOUNCEMENT_SCHEDULED = "flock.announcement.scheduled"
ANNOUNCEMENT_PUBLISHED = "flock.announcement.published"
NOTIFICATION_SENT = "flock.notification.sent"
APPROVAL_REQUESTED = "flock.approval.requested"
APPROVAL_STEP_APPROVED = "flock.approval.step_approved"
APPROVAL_STEP_REJECTED = "flock.approval.step_rejected"
APPROVAL_APPROVED = "flock.approval.approved"
APPROVAL_REJECTED = "flock.approval.rejected"
"""One-time-event approval lifecycle (FLO-7 §7, materialized by [FLO-61]).
Granular per-step + terminal events so notification fan-out ([FLO-8](/FLO/issues/FLO-8))
and the registration gate ([FLO-62](/FLO/issues/FLO-62)) react to the exact
transition: ``requested`` on submit, ``step_approved``/``step_rejected`` per
approver, ``approved``/``rejected`` on the terminal decision. Replaces the
earlier coarse ``flock.approval.decided`` placeholder with the spec's canonical
granular set."""
REGISTRATION_OPENED = "flock.registration.opened"
REGISTRATION_CREATED = "flock.registration.created"
REGISTRATION_WAITLISTED = "flock.registration.waitlisted"
REGISTRATION_CANCELLED = "flock.registration.cancelled"
REGISTRATION_CHECKED_IN = "flock.registration.checked_in"
"""Scoped registration lifecycle (FLO-7 §5 / §7, materialized by [FLO-62]).
``opened`` fires on final approval when the window + confirmed scope land on
the gathering (drives the FLO-8 announcement + the eligibility gate);
``created``/``waitlisted`` per registration (capacity hit → waitlist);
``cancelled`` on a registrant cancel; ``checked_in`` on the FLO-6 attendance
bridge. Routed through the single sanctioned :func:`emit` so the
``Flock Event Outbox`` + notification fan-out stay coherent."""

#: Redis pub/sub channel prefix for domain events (ADR-0001 §5.1).
PUBSUB_CHANNEL_PREFIX = "flock"


def pubsub_channel(event: str) -> str:
	"""The Redis pub/sub channel a domain event is published to (``flock:<event>``)."""
	return f"{PUBSUB_CHANNEL_PREFIX}:{event}"


@dataclass(frozen=True)
class DomainEvent:
	"""A domain event emitted by a state change (Frappe hook + Redis pub/sub).

	``scope`` carries the row-level anchors (``branch`` / ``group`` /
	``organization``) so subscribers and realtime fan-out can derive rooms without
	re-querying the DB (event-modeling rule, AGENTS.md).
	"""

	name: str
	payload: dict[str, Any] = field(default_factory=dict)
	scope: dict[str, Any] = field(default_factory=dict)


# Type alias for a subscriber filter predicate.
FilterFn = Callable[[DomainEvent], bool]
# Type alias for a subscriber handler.
Handler = Callable[[DomainEvent], None]


@runtime_checkable
class EventSink(Protocol):
	"""Port: the durable + realtime side-effects of :func:`emit`.

	Production adapter: :class:`FrappeEventSink` (Redis pub/sub via
	``frappe.publish_realtime`` + ``Flock Event Outbox`` write). Unit tests use
	:class:`RecordingEventSink`. Keeping the side effects behind a port keeps
	:func:`emit` free of Frappe/Redis so the event bus is unit-testable in
	isolation (ADR-0001 §2 separation of concerns).
	"""

	def publish(self, event: DomainEvent, *, realtime: bool, room: str | None) -> None:
		"""Push the event to Redis pub/sub + Frappe realtime room + outbox."""
		...


class NullEventSink:
	"""A sink that records nothing — used as the default before wiring."""

	def publish(self, event: DomainEvent, *, realtime: bool, room: str | None) -> None:  # noqa: ARG002
		return None


class RecordingEventSink:
	"""In-memory sink for unit tests: captures every published event + room opts."""

	def __init__(self) -> None:
		self.published: list[tuple[DomainEvent, bool, str | None]] = []

	def publish(self, event: DomainEvent, *, realtime: bool, room: str | None) -> None:
		self.published.append((event, realtime, room))


class FrappeEventSink:
	"""Production sink: Redis pub/sub + Frappe realtime + outbox (ADR-0001 §5.1).

	Lazily imports Frappe so this module stays import-clean in CI (no bench).
	All Redis access goes through ``frappe.publish_realtime`` — no bespoke Redis
	clients (FLO-10 §6). The ``Flock Event Outbox`` DocType is the durability
	floor; it lands with the data model ([FLO-17](/FLO/issues/FLO-17)) and the
	write degrades gracefully (best-effort log) until then.
	"""

	OUTBOX_DOCTYPE = "Flock Event Outbox"

	@property
	def _frappe(self):  # type: ignore[no-untyped-def]
		import frappe

		return frappe

	def publish(self, event: DomainEvent, *, realtime: bool, room: str | None) -> None:
		frappe = self._frappe
		# Redis pub/sub backbone + browser websocket fan-out — the only sanctioned
		# Redis-touching call (FLO-10 §6). ``room`` scopes which clients receive it.
		if realtime:
			frappe.publish_realtime(
				event.name,
				message={"name": event.name, "payload": dict(event.payload), "scope": dict(event.scope)},
				room=room,
			)
		self._append_outbox(frappe, event)

	def _append_outbox(self, frappe, event: DomainEvent) -> None:  # type: ignore[no-untyped-def]
		# Durability floor: at-least-once replay (ADR-0001 §5.1). Best-effort until
		# the outbox DocType ships; realtime owns UX, the queue owns correctness.
		try:
			frappe.get_doc(
				{
					"doctype": self.OUTBOX_DOCTYPE,
					"event": event.name,
					"payload": str(event.payload),
					"scope": str(event.scope),
				}
			).db_insert()
		except Exception:
			frappe.log_error(f"flock_os.events outbox append failed: {event.name}")


@dataclass
class _Subscription:
	"""A registered subscriber: handler + optional filter predicate."""

	handler: Handler
	filter_fn: FilterFn | None


class EventBus:
	"""In-process subscriber registry + dispatcher — the single publisher core.

	Subscribers are dispatched **synchronously** within :func:`emit` by default.
	Per ADR-0001 §5.2 subscribers run on RQ workers in production "except for
	cheap idempotent updates"; the realtime fan-out projector (FLO-14) is exactly
	such a cheap best-effort update, so inline dispatch is correct here. Heavy
	subscribers (rollup rebuilds, exports) self-enqueue onto RQ inside their
	handler. This keeps the hot emit path fast and the bus unit-testable.
	"""

	def __init__(self, sink: EventSink | None = None) -> None:
		self._sink: EventSink = sink or NullEventSink()
		self._subs: dict[str, list[_Subscription]] = {}

	def install_sink(self, sink: EventSink) -> None:
		"""Swap the side-effecting sink (production wiring / tests)."""
		self._sink = sink

	def subscribe(self, event: str, handler: Handler, *, filter_fn: FilterFn | None = None) -> Handler:
		"""Register ``handler`` for ``event``. Returns ``handler`` for chaining.

		A subscriber receives every emitted event whose name matches and whose
		``filter_fn`` (if any) returns truthy. Handlers must be idempotent
		(outbox → at-least-once delivery, ADR-0001 §5.2).
		"""
		self._subs.setdefault(event, []).append(_Subscription(handler, filter_fn))
		return handler

	def subscriptions(self, event: str) -> Iterable[_Subscription]:
		"""Read-only view of subscribers for ``event`` (test/audit introspection)."""
		return tuple(self._subs.get(event, ()))

	def emit(
		self,
		event: str,
		*,
		payload: dict[str, Any] | None = None,
		scope: dict[str, Any] | None = None,
		realtime: bool = True,
		room: str | None = None,
	) -> DomainEvent:
		"""Publish a domain event — the single sanctioned entry point.

		Order (ADR-0001 §5.1): dispatch to in-process subscribers, then push to
		the sink (Redis pub/sub + Frappe realtime room + outbox). The sink is
		best-effort; a realtime failure never fails the originating request
		(correctness lives in the queue, FLO-10 §5.3).

		Returns the materialized :class:`DomainEvent` (handy for tests + logging).
		"""
		domain_event = DomainEvent(name=event, payload=dict(payload or {}), scope=dict(scope or {}))
		# Dispatch to subscribers first (cheap idempotent updates, inline).
		for sub in tuple(self._subs.get(event, ())):
			if sub.filter_fn is not None and not sub.filter_fn(domain_event):
				continue
			sub.handler(domain_event)
		# Side effects: pub/sub + realtime + outbox (best-effort, never raises
		# into the caller's request path).
		try:
			self._sink.publish(domain_event, realtime=realtime, room=room)
		except Exception:  # noqa: BLE001 - realtime is best-effort (FLO-10 §5.3)
			pass
		return domain_event


# ---------------------------------------------------------------------------- #
# Module-level bus + convenience API.
#
# The module-level :data:`_bus` is the single process-wide registry. Production
# wiring (hooks.py) installs :class:`FrappeEventSink`; tests swap in a recording
# sink. :func:`emit` / :func:`subscribe` are the stable public surface every
# feature imports — e.g. ``from flock_os.events import emit`` (FLO-15 reporting).
# ---------------------------------------------------------------------------- #
_bus = EventBus()


def install_sink(sink: EventSink) -> None:
	"""Install the production/test sink on the module-level bus."""
	_bus.install_sink(sink)


def subscribe(event: str, handler: Handler, *, filter_fn: FilterFn | None = None) -> Handler:
	"""Register a subscriber on the module-level bus."""
	return _bus.subscribe(event, handler, filter_fn=filter_fn)


def emit(
	event: str,
	*,
	payload: dict[str, Any] | None = None,
	scope: dict[str, Any] | None = None,
	realtime: bool = True,
	room: str | None = None,
) -> DomainEvent:
	"""Publish a domain event via the module-level bus (single sanctioned path)."""
	return _bus.emit(event, payload=payload, scope=scope, realtime=realtime, room=room)


def on_doc_event(doctype: str, event_name: str) -> None:
	"""Entry point for Frappe ``doc_events`` hooks (hooks.py wiring).

	Emits a domain event for a DocType lifecycle transition. The actual DocType
	→ event-name mapping is wired in ``hooks.py`` (the only place Frappe is
	wired, ADR-0001 §2). This helper keeps the hook table declarative.
	"""
	# Imported lazily: doc-event hooks only fire inside a running bench.
	import frappe

	doc = frappe.get_doc(doctype) if isinstance(doctype, str) else doctype
	emit(
		event_name,
		payload={"doctype": doc.doctype, "name": doc.name},
		scope=_scope_from_doc(doc),
	)


def _scope_from_doc(doc) -> dict[str, Any]:  # type: ignore[no-untyped-def]
	"""Derive the row-level scope anchors (branch/group/organization) from a doc."""
	scope: dict[str, Any] = {}
	for key in ("branch", "group", "organization"):
		value = getattr(doc, key, None)
		if value:
			scope[key] = value
	return scope
