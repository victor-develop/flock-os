"""
Realtime fan-out: sharded event-room channels + domain-event projector (FLO-14).

Implements the realtime transport layer defined in the FLO-10 scale ADR §5
([design](/FLO/issues/FLO-10#document-design)) against the event catalog in
ADR-0001 §5 ([FLO-4](/FLO/issues/FLO-4)). This is the fan-out backbone for
[FLO-9](/FLO/issues/FLO-9) (live games / questionnaires) and
[FLO-8](/FLO/issues/FLO-8) (scoped push notifications).

Two responsibilities:

1. **Sharded event-room channel naming** (§5.1) — the single contract the
   Architect owns. A 15k-attendee room is sharded into N≈10 sub-channels so a
   single Redis pub/sub channel never serializes on a 15k-subscriber fan-out;
   clients join exactly one shard for presence/room traffic plus the shared
   broadcast channel for admin pushes.

2. **The realtime projector** (§5.2) — a thin subscriber that translates each
   domain event (``attendance.recorded``, ``engagement.session.closed``, …) into
   the correct shard(s) + broadcast fan-out via ``frappe.publish_realtime``.
   Features **subscribe to events — they never poll or re-query** the DB
   (event-modeling rule, AGENTS.md).

Transport-agnostic + import-clean without a Frappe site: the
:class:`RealtimePublisher` port wraps ``frappe.publish_realtime`` in production
(the only sanctioned Redis-touching path, FLO-10 §6) and a recording fake in the
unit suite. The channel-naming + shard-assignment + routing logic is pure
Python, so the DoD projector test runs under plain ``pytest`` with no bench.

Backpressure (§5.3): reporter acks are synchronous + cheap (handled in
:mod:`flock_os.attendance`); room updates emitted here are best-effort. If
realtime lags, attendance is still durable — correctness stays in the queue
([FLO-15](/FLO/issues/FLO-15)); realtime owns UX.
"""

from __future__ import annotations

import zlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from flock_os import events
from flock_os.events import DomainEvent

# ---------------------------------------------------------------------------- #
# Realtime event names — the client-side websocket events the browser listens
# for (first arg to ``frappe.publish_realtime``). Distinct from the domain-event
# catalog (what the server emits); the projector maps domain event → realtime
# event + room.
# ---------------------------------------------------------------------------- #
RT_ATTENDANCE_PRESENCE = "flock_os:attendance:presence"
"""A single attendee presence change, shard-targeted (who just checked in)."""

RT_ATTENDANCE_COUNT = "flock_os:attendance:count"
"""Aggregate attendance-count tick, broadcast to the whole event room."""

RT_GAME_STATE = "flock_os:engagement:state"
"""Live-game lifecycle: opened / closed / results — all shards + broadcast."""

RT_NOTIFICATION = "flock_os:notification"
"""Admin / scoped push, broadcast channel only (ADR-0001 §5.1)."""

# ---------------------------------------------------------------------------- #
# Channel naming (FLO-10 §5.1) — the single Architect-owned contract.
# ---------------------------------------------------------------------------- #
EVENT_ROOM_PREFIX = "flock_os:event"
BROADCAST_SEGMENT = "broadcast"
SHARD_SEGMENT = "shard"

DEFAULT_SHARD_COUNT = 10
"""Default room shard count (FLO-10 §5.1: N≈10 → ~1.5k subscribers/shard at 15k).
Tunable per org via ``Flock Organization`` settings (config-over-constants,
ADR-0001 §7) once that DocType lands."""


def broadcast_channel(event_id: str) -> str:
	"""The shared broadcast room for an event (admin pushes + room-wide updates).

	Every client in the event room subscribes to this in addition to its shard.
	"""
	return f"{EVENT_ROOM_PREFIX}:{event_id}:{BROADCAST_SEGMENT}"


def shard_channel(event_id: str, shard: int) -> str:
	"""One of the N presence/room shard rooms for an event (§5.1)."""
	return f"{EVENT_ROOM_PREFIX}:{event_id}:{SHARD_SEGMENT}:{shard}"


def shard_for(attendee_ref: str, shard_count: int = DEFAULT_SHARD_COUNT) -> int:
	"""Stable shard assignment for an attendee: ``hash(attendee) mod N`` (§5.1).

	Uses ``zlib.crc32`` so the assignment is **stable across processes and
	languages** — the browser must compute the same shard to know which room to
	join. (Python's builtin ``hash()`` is per-process salted and cannot be used
	here.) The JS parity contract is ``crc32(utf8(attendee_ref)) >>> 0) % N``.
	"""
	return zlib.crc32(attendee_ref.encode("utf-8")) % shard_count


def shards_for(attendee_refs: Sequence[str], shard_count: int = DEFAULT_SHARD_COUNT) -> list[int]:
	"""The sorted, de-duplicated set of shards a group of attendees lands in."""
	return sorted({shard_for(ref, shard_count) for ref in attendee_refs})


def all_shards(shard_count: int = DEFAULT_SHARD_COUNT) -> list[int]:
	"""Every shard index — used when a fan-out must reach the whole room."""
	return list(range(shard_count))


# ---------------------------------------------------------------------------- #
# Publisher port (hexagonal) — production wraps frappe.publish_realtime.
# ---------------------------------------------------------------------------- #
@runtime_checkable
class RealtimePublisher(Protocol):
	"""Port: publish one realtime event to one websocket room.

	Production: :class:`FrappeRealtimePublisher` (the only sanctioned
	Redis-touching path, FLO-10 §6). Unit tests: :class:`RecordingRealtimePublisher`.
	"""

	def publish(self, *, event: str, room: str, message: dict[str, Any]) -> None:
		"""Push ``message`` to every client listening on ``room`` for ``event``."""
		...


class NullRealtimePublisher:
	"""No-op publisher (default before the projector is wired)."""

	def publish(self, *, event: str, room: str, message: dict[str, Any]) -> None:  # noqa: ARG002
		return None


class RecordingRealtimePublisher:
	"""In-memory publisher for unit tests: records every (event, room, message)."""

	def __init__(self) -> None:
		self.calls: list[dict[str, Any]] = []

	def publish(self, *, event: str, room: str, message: dict[str, Any]) -> None:
		self.calls.append({"event": event, "room": room, "message": dict(message)})

	def rooms(self, event: str | None = None) -> list[str]:
		"""All rooms published to, optionally filtered by realtime event name."""
		return [c["room"] for c in self.calls if event is None or c["event"] == event]


class FrappeRealtimePublisher:
	"""Production adapter wrapping ``frappe.publish_realtime`` (FLO-10 §6).

	Lazily imports Frappe so this module stays import-clean in CI (no bench).
	No bespoke Redis clients — the D3 cluster escape hatch stays viable.
	"""

	@property
	def _frappe(self):  # type: ignore[no-untyped-def]
		import frappe

		return frappe

	def publish(self, *, event: str, room: str, message: dict[str, Any]) -> None:
		self._frappe.publish_realtime(event, message=message, room=room)


# ---------------------------------------------------------------------------- #
# Realtime fan-out record — one published target (audit/test surface).
# ---------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RealtimeEmission:
	"""One fan-out target produced by the projector for a single domain event."""

	realtime_event: str
	"""The client-side websocket event name (``frappe.publish_realtime`` arg 1)."""

	room: str
	"""The channel published to — a shard room or the broadcast room (§5.1)."""

	message: dict[str, Any]
	"""The payload delivered to clients in ``room``."""


# Type alias for a per-event routing handler.
RouteHandler = Callable[["EventRoomProjector", DomainEvent], list[RealtimeEmission]]


class EventRoomProjector:
	"""Thin domain-event to sharded-realtime projector (FLO-10 §5.2).

	Subscribes to the event catalog (ADR-0001 §5.4) and routes each event to the
	correct shard room(s) + the broadcast room via the publisher port. It never
	re-queries the DB — every fact it needs is in the event payload/scope
	(event-modeling rule, AGENTS.md).

	Routing rules (§5.1):

	- ``attendance.recorded`` → attendee's shard (presence) + broadcast (count tick).
	- ``attendance.bulk_recorded`` → touched-or-all shards + broadcast (the FLO-9 game-close path, DoD #2).
	- ``engagement.session.opened/closed`` → all shards + broadcast (game state change).
	- ``notification.sent`` / ``announcement.scheduled`` → broadcast only (admin push).
	"""

	def __init__(
		self,
		publisher: RealtimePublisher | None = None,
		shard_count: int = DEFAULT_SHARD_COUNT,
	) -> None:
		self._publisher: RealtimePublisher = publisher or NullRealtimePublisher()
		self.shard_count = shard_count
		self._routes: dict[str, RouteHandler] = self._build_routes()

	@property
	def publisher(self) -> RealtimePublisher:
		return self._publisher

	def install_publisher(self, publisher: RealtimePublisher) -> None:
		"""Swap the publisher (production wiring / tests)."""
		self._publisher = publisher

	# -- public API ---------------------------------------------------------- #
	def project(self, event: DomainEvent) -> list[RealtimeEmission]:
		"""Route ``event`` to its realtime channel(s); publish + return emissions.

		Unknown events are a no-op (the projector only routes the cataloged names
		it owns). Best-effort: a publish failure never propagates (§5.3).
		"""
		handler = self._routes.get(event.name)
		if handler is None:
			return []
		emissions = handler(self, event)
		for emission in emissions:
			try:
				self._publisher.publish(
					event=emission.realtime_event,
					room=emission.room,
					message=emission.message,
				)
			except Exception:  # noqa: BLE001 - realtime is best-effort (FLO-10 §5.3)
				pass
		return emissions

	def handle(self, event: DomainEvent) -> None:
		"""Subscriber-compatible entry point (matches the ``events.subscribe`` shape)."""
		self.project(event)

	@property
	def routed_events(self) -> tuple[str, ...]:
		"""Cataloged event names this projector routes (audit/introspection)."""
		return tuple(self._routes)

	# -- wiring -------------------------------------------------------------- #
	def register(self, bus: Any = events) -> None:
		"""Subscribe the projector to every routed event on the event bus.

		Call once at app import (``hooks.py`` wiring, §5.2). Idempotent across
		re-registration within a process because Frappe reloads modules rarely.
		"""
		for name in self._routes:
			bus.subscribe(name, self.handle)

	# -- channel helpers ----------------------------------------------------- #
	def broadcast_channel(self, event: DomainEvent) -> str | None:
		"""The broadcast room for the event's gathering, or ``None`` if absent."""
		event_id = _gathering_id(event)
		return broadcast_channel(event_id) if event_id else None

	def touched_shards(self, event: DomainEvent) -> list[int]:
		"""Shards the projector must fan to for ``event`` (routing-rule dependent)."""
		return all_shards(self.shard_count)

	# -- routing table (§5.1 / §5.2) ----------------------------------------- #
	def _build_routes(self) -> dict[str, RouteHandler]:
		return {
			events.ATTENDANCE_RECORDED: _route_single_attendance,
			events.ATTENDANCE_BULK_RECORDED: _route_bulk_attendance,
			events.ENGAGEMENT_SESSION_OPENED: _route_game_lifecycle,
			events.ENGAGEMENT_SESSION_CLOSED: _route_game_lifecycle,
			events.NOTIFICATION_SENT: _route_broadcast_only(RT_NOTIFICATION),
			events.ANNOUNCEMENT_SCHEDULED: _route_broadcast_only(RT_NOTIFICATION),
			events.GATHERING_APPROVED: _route_broadcast_only(RT_NOTIFICATION),
		}


# ---------------------------------------------------------------------------- #
# Routing handlers. Each returns the full emission list (shard(s) + broadcast);
# the projector publishes them. Pure functions of (projector, event) — no I/O.
# ---------------------------------------------------------------------------- #
def _gathering_id(event: DomainEvent) -> str | None:
	"""Resolve the gathering/event id from an event payload or scope.

	The catalog uses ``gathering`` as the canonical key (ADR-0001 §5.4,
	FLO-6 §7); ``event`` is accepted as a legacy alias.
	"""
	for key in ("gathering", "event"):
		value = event.payload.get(key) or event.scope.get(key)
		if value:
			return str(value)
	return None


def _attendee_refs(event: DomainEvent) -> list[str]:
	"""Pull attendee refs from a payload if the emitter included them.

	Bulk/engagement events *may* carry ``attendees``/``members``/``players`` to
	enable precise shard targeting; if absent the projector fans to all shards.
	"""
	refs: list[str] = []
	for key in ("attendees", "members", "players", "attendee_refs"):
		value = event.payload.get(key)
		if isinstance(value, (list, tuple)):
			refs.extend(str(v) for v in value if v)
	return refs


def _presence_message(event: DomainEvent, *, delta: int) -> dict[str, Any]:
	"""Build the broadcast count-tick message for an attendance event."""
	message: dict[str, Any] = {
		"delta": delta,
		"source": event.payload.get("source", "reporting"),
	}
	for key in ("gathering", "branch", "group"):
		if event.payload.get(key):
			message[key] = event.payload[key]
	return message


def _route_single_attendance(projector: EventRoomProjector, event: DomainEvent) -> list[RealtimeEmission]:
	"""``flock.attendance.recorded`` → the attendee's shard (presence) + broadcast."""
	emissions: list[RealtimeEmission] = []
	event_id = _gathering_id(event)
	member = str(event.payload.get("member") or event.payload.get("attendee_ref") or "")
	if event_id and member:
		shard = shard_for(member, projector.shard_count)
		emissions.append(
			RealtimeEmission(
				realtime_event=RT_ATTENDANCE_PRESENCE,
				room=shard_channel(event_id, shard),
				message={"gathering": event_id, "member": member, **_extra(event)},
			)
		)
	if event_id:
		emissions.append(
			RealtimeEmission(
				realtime_event=RT_ATTENDANCE_COUNT,
				room=broadcast_channel(event_id),
				message=_presence_message(event, delta=1),
			)
		)
	return emissions


def _route_bulk_attendance(projector: EventRoomProjector, event: DomainEvent) -> list[RealtimeEmission]:
	"""``flock.attendance.bulk_recorded`` → touched shards (or all) + broadcast.

	This is the fan-out path a FLO-9 live-game completion flows through: the
	engagement session close records its players as attendees via the bulk
	reporting service (FLO-15), which emits this event; the projector then fans
	the room update out to the relevant shards + broadcast (FLO-14 DoD #2).
	"""
	event_id = _gathering_id(event)
	if not event_id:
		return []
	count = int(event.payload.get("count", 0) or 0)
	refs = _attendee_refs(event)
	target_shards = shards_for(refs, projector.shard_count) if refs else all_shards(projector.shard_count)
	emissions: list[RealtimeEmission] = [
		RealtimeEmission(
			realtime_event=RT_ATTENDANCE_PRESENCE,
			room=shard_channel(event_id, shard),
			message={"gathering": event_id, "batch_id": event.payload.get("batch_id")},
		)
		for shard in target_shards
	]
	emissions.append(
		RealtimeEmission(
			realtime_event=RT_ATTENDANCE_COUNT,
			room=broadcast_channel(event_id),
			message=_presence_message(event, delta=count),
		)
	)
	return emissions


def _route_game_lifecycle(projector: EventRoomProjector, event: DomainEvent) -> list[RealtimeEmission]:
	"""``flock.engagement.session.opened/closed`` → all shards + broadcast.

	Every player — whichever shard they are on — must see the game start/finish
	and results, so this fans to all N shards plus the broadcast room (§5.1).
	"""
	event_id = _gathering_id(event)
	if not event_id:
		return []
	state = "opened" if event.name == events.ENGAGEMENT_SESSION_OPENED else "closed"
	message: dict[str, Any] = {
		"gathering": event_id,
		"state": state,
		**_extra(event),
	}
	emissions = [
		RealtimeEmission(
			realtime_event=RT_GAME_STATE,
			room=shard_channel(event_id, shard),
			message=message,
		)
		for shard in all_shards(projector.shard_count)
	]
	emissions.append(
		RealtimeEmission(
			realtime_event=RT_GAME_STATE,
			room=broadcast_channel(event_id),
			message=message,
		)
	)
	return emissions


def _route_broadcast_only(realtime_event: str) -> RouteHandler:
	"""Build a handler that fans to the broadcast room only (admin pushes, §5.1)."""

	def handler(projector: EventRoomProjector, event: DomainEvent) -> list[RealtimeEmission]:
		event_id = _gathering_id(event)
		if not event_id:
			return []
		return [
			RealtimeEmission(
				realtime_event=realtime_event,
				room=broadcast_channel(event_id),
				message={"gathering": event_id, **_extra(event)},
			)
		]

	return handler


def _extra(event: DomainEvent) -> dict[str, Any]:
	"""Forward non-structural payload keys into the realtime message verbatim."""
	skip = {"gathering", "event", "member", "attendee_ref", "count", "batch_id"}
	return {k: v for k, v in event.payload.items() if k not in skip}


# ---------------------------------------------------------------------------- #
# Module-level projector + wiring helpers.
# ---------------------------------------------------------------------------- #
_projector: EventRoomProjector | None = None


def get_projector() -> EventRoomProjector:
	"""The process-wide projector instance (lazily built, singleton per process)."""
	global _projector
	if _projector is None:
		_projector = EventRoomProjector()
	return _projector


def install_publisher(publisher: RealtimePublisher) -> None:
	"""Install the production/test publisher on the module-level projector."""
	get_projector().install_publisher(publisher)


def register_projector(bus: Any = events) -> EventRoomProjector:
	"""Build the projector, install the Frappe publisher, and subscribe it.

	The single wiring entry point called from ``hooks.py`` (§5.2). In a bench it
	installs :class:`FrappeRealtimePublisher`; the unit suite swaps in a
	recording publisher and calls :meth:`EventRoomProjector.register` directly.
	"""
	projector = get_projector()
	projector.install_publisher(FrappeRealtimePublisher())
	projector.register(bus)
	return projector
