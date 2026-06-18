"""
Realtime fan-out unit tests — FLO-14 Definition of Done.

These pin the FLO-10 ADR §5 realtime layer contract under plain ``pytest``
(no Frappe site / Redis / bench), mirroring the SQL-light project-level pattern
from ``test_scale_attendance.py``:

* sharded event-room channel naming (§5.1)
* stable, cross-process shard assignment (``hash(attendee) mod N``)
* the projector routes a domain event to the expected shard(s) + broadcast (DoD #1)
* the FLO-9 live-game completion → attendance fan-out path is wired end-to-end
  through the single sanctioned emitter (DoD #2)
* admin push goes to broadcast only; bulk attendance fans the touched shards
* best-effort realtime: a publisher failure never propagates (§5.3)
"""

from __future__ import annotations

import zlib

import flock_os.events as events
from flock_os.events import DomainEvent, EventBus, RecordingEventSink
from flock_os.realtime import (
	BROADCAST_SEGMENT,
	DEFAULT_SHARD_COUNT,
	EVENT_ROOM_PREFIX,
	RT_ATTENDANCE_COUNT,
	RT_ATTENDANCE_PRESENCE,
	RT_GAME_STATE,
	RT_NOTIFICATION,
	EventRoomProjector,
	RecordingRealtimePublisher,
	all_shards,
	broadcast_channel,
	get_projector,
	shard_channel,
	shard_for,
	shards_for,
)

GATHERING = "gathering-42"


# --------------------------------------------------------------------------- #
# Channel naming (§5.1) — the Architect-owned contract.
# --------------------------------------------------------------------------- #
def test_broadcast_channel_matches_naming_contract():
	assert broadcast_channel(GATHERING) == f"{EVENT_ROOM_PREFIX}:{GATHERING}:{BROADCAST_SEGMENT}"


def test_shard_channel_matches_naming_contract():
	assert shard_channel(GATHERING, 3) == f"{EVENT_ROOM_PREFIX}:{GATHERING}:shard:3"


def test_shard_for_is_stable_and_within_range():
	# Same attendee always lands in the same shard (stable across calls).
	a = shard_for("member-7")
	b = shard_for("member-7")
	assert a == b
	assert 0 <= a < DEFAULT_SHARD_COUNT


def test_shard_for_matches_documented_crc32_contract():
	# The documented cross-language contract: crc32(utf8(ref)) >>> 0) % N.
	ref = "member-7"
	assert shard_for(ref) == (zlib.crc32(ref.encode("utf-8")) % DEFAULT_SHARD_COUNT)


def test_shard_for_distributes_attendees_across_shards():
	assigned = {shard_for(f"member-{i}") for i in range(2_000)}
	# 2k attendees over N=10 must touch every shard (uniform-ish distribution).
	assert assigned == set(all_shards())


def test_shards_for_dedupes_and_sorts():
	refs = [f"member-{i}" for i in range(500)]
	assert shards_for(refs) == sorted({shard_for(r) for r in refs})


def test_shard_count_is_configurable():
	# A 5-shard room partitions differently than the default 10.
	assert shard_for("member-7", shard_count=5) == (zlib.crc32(b"member-7") % 5)
	assert len(all_shards(5)) == 5


# --------------------------------------------------------------------------- #
# DoD #1 — projector routes a domain event to the expected shard(s) + broadcast.
# --------------------------------------------------------------------------- #
def test_single_attendance_routes_to_attendee_shard_and_broadcast():
	"""``flock.attendance.recorded`` → attendee's shard (presence) + broadcast (count)."""
	publisher = RecordingRealtimePublisher()
	projector = EventRoomProjector(publisher)

	member = "member-7"
	emissions = projector.project(
		DomainEvent(
			events.ATTENDANCE_RECORDED,
			{"gathering": GATHERING, "member": member, "branch": "branch-a"},
		)
	)

	expected_shard = shard_for(member)
	rooms = [e.room for e in emissions]
	assert shard_channel(GATHERING, expected_shard) in rooms  # presence → shard
	assert broadcast_channel(GATHERING) in rooms  # count → broadcast

	# Presence emission targets the right realtime event + shard.
	presence = next(e for e in emissions if e.realtime_event == RT_ATTENDANCE_PRESENCE)
	assert presence.room == shard_channel(GATHERING, expected_shard)
	assert presence.message["member"] == member

	# Count tick lands on broadcast with delta=1.
	count = next(e for e in emissions if e.realtime_event == RT_ATTENDANCE_COUNT)
	assert count.room == broadcast_channel(GATHERING)
	assert count.message["delta"] == 1

	# The publisher actually received both (project() publishes as it routes).
	assert {c["room"] for c in publisher.calls} == {
		shard_channel(GATHERING, expected_shard),
		broadcast_channel(GATHERING),
	}


def test_bulk_attendance_with_refs_targets_only_touched_shards_plus_broadcast():
	"""Bulk event carrying attendee refs fans only the touched shards + broadcast."""
	publisher = RecordingRealtimePublisher()
	projector = EventRoomProjector(publisher)

	refs = ["member-0", "member-1", "member-2"]
	touched = set(shards_for(refs))

	emissions = projector.project(
		DomainEvent(
			events.ATTENDANCE_BULK_RECORDED,
			{
				"gathering": GATHERING,
				"count": len(refs),
				"batch_id": "b-1",
				"branch": "branch-a",
				"attendees": refs,
			},
		)
	)

	shard_rooms = {e.room for e in emissions if e.realtime_event == RT_ATTENDANCE_PRESENCE}
	assert shard_rooms == {shard_channel(GATHERING, s) for s in touched}
	# Untouched shards were NOT fanned to (cheap fan-out, §5.1).
	assert all(shard_channel(GATHERING, s) not in shard_rooms for s in all_shards() if s not in touched)

	count = next(e for e in emissions if e.realtime_event == RT_ATTENDANCE_COUNT)
	assert count.room == broadcast_channel(GATHERING)
	assert count.message["delta"] == len(refs)


def test_bulk_attendance_without_refs_fans_all_shards_plus_broadcast():
	"""Bulk event without attendee refs fans every shard (count is room-wide)."""
	publisher = RecordingRealtimePublisher()
	projector = EventRoomProjector(publisher)

	emissions = projector.project(
		DomainEvent(
			events.ATTENDANCE_BULK_RECORDED,
			{"gathering": GATHERING, "count": 15_000, "batch_id": "b-2", "branch": "branch-a"},
		)
	)

	shard_rooms = {e.room for e in emissions if e.realtime_event == RT_ATTENDANCE_PRESENCE}
	# All N shards fanned (10 publishes is trivially cheap vs 15k fan-out, §5.1).
	assert shard_rooms == {shard_channel(GATHERING, s) for s in all_shards()}
	assert broadcast_channel(GATHERING) in [e.room for e in emissions]


def test_admin_push_goes_to_broadcast_only():
	"""``flock.notification.sent`` → broadcast channel only (never a shard)."""
	publisher = RecordingRealtimePublisher()
	projector = EventRoomProjector(publisher)

	emissions = projector.project(
		DomainEvent(
			events.NOTIFICATION_SENT,
			{"gathering": GATHERING, "branch": "branch-a", "audience_size": 9000},
		)
	)

	assert len(emissions) == 1
	assert emissions[0].room == broadcast_channel(GATHERING)
	assert emissions[0].realtime_event == RT_NOTIFICATION
	# No shard channel was touched.
	assert all(":shard:" not in e.room for e in emissions)


def test_unknown_event_is_a_noop():
	publisher = RecordingRealtimePublisher()
	projector = EventRoomProjector(publisher)

	assert projector.project(DomainEvent("flock.something.else", {"gathering": GATHERING})) == []
	assert publisher.calls == []


def test_event_without_gathering_emits_nothing():
	"""An attendance event missing its gathering id cannot route — drop it."""
	projector = EventRoomProjector(RecordingRealtimePublisher())
	assert projector.project(DomainEvent(events.ATTENDANCE_RECORDED, {"member": "x"})) == []


# --------------------------------------------------------------------------- #
# §5.3 — realtime is best-effort: a publish failure never propagates.
# --------------------------------------------------------------------------- #
def test_publisher_failure_does_not_propagate():
	class BoomPublisher:
		def publish(self, *, event, room, message):  # noqa: ARG002
			raise RuntimeError("redis is gone")

	projector = EventRoomProjector(BoomPublisher())  # type: ignore[arg-type]
	# Should not raise — realtime owns UX, not correctness (§5.3).
	emissions = projector.project(
		DomainEvent(events.ATTENDANCE_RECORDED, {"gathering": GATHERING, "member": "m"})
	)
	assert emissions  # routing still produced the fan-out plan (audit surface).


# --------------------------------------------------------------------------- #
# DoD #2 — FLO-9 live-game completion records players as attendees via this path.
# The engagement session close → bulk reporting → bulk_recorded → projector flow.
# --------------------------------------------------------------------------- #
def test_live_game_close_fans_all_shards_and_broadcast():
	"""``flock.engagement.session.closed`` reaches every player on every shard."""
	publisher = RecordingRealtimePublisher()
	projector = EventRoomProjector(publisher)

	emissions = projector.project(
		DomainEvent(
			events.ENGAGEMENT_SESSION_CLOSED,
			{"gathering": GATHERING, "session": "game-1", "branch": "branch-a"},
		)
	)

	state_rooms = {e.room for e in emissions if e.realtime_event == RT_GAME_STATE}
	# All shards + broadcast — a game-over must reach every player (§5.1).
	assert state_rooms == {shard_channel(GATHERING, s) for s in all_shards()} | {broadcast_channel(GATHERING)}
	msg = emissions[0].message
	assert msg["state"] == "closed"


def test_live_game_completion_to_attendance_fanout_end_to_end():
	"""End-to-end: game close records players via bulk path → projector fans the room.

	This wires DoD #2: a FLO-9 engagement session close records its players as
	attendees through the bulk reporting service (FLO-15), which emits one
	``flock.attendance.bulk_recorded`` via the single sanctioned emitter; the
	projector — subscribed on the bus — fans the room update out to the touched
	shards + broadcast. No polling, no re-querying (event-modeling rule).
	"""
	bus = EventBus(sink=RecordingEventSink())
	publisher = RecordingRealtimePublisher()
	projector = EventRoomProjector(publisher)
	projector.register(bus)

	players = [f"player-{i}" for i in range(250)]
	touched = set(shards_for(players))

	# The bulk reporting service (FLO-15) emits this after recording the players.
	bus.emit(
		events.ATTENDANCE_BULK_RECORDED,
		payload={
			"gathering": GATHERING,
			"count": len(players),
			"batch_id": "game-1-close",
			"branch": "branch-a",
			"attendees": players,  # enables precise shard targeting
		},
	)

	presence_rooms = publisher.rooms(RT_ATTENDANCE_PRESENCE)
	assert set(presence_rooms) == {shard_channel(GATHERING, s) for s in touched}
	assert broadcast_channel(GATHERING) in publisher.rooms(RT_ATTENDANCE_COUNT)


# --------------------------------------------------------------------------- #
# Emitter + projector integration (the single sanctioned publish path, ADR §5.1).
# --------------------------------------------------------------------------- #
def test_emit_routes_to_projector_when_registered():
	"""emit() dispatches to the subscribed projector → realtime fan-out happens."""
	bus = EventBus(sink=RecordingEventSink())
	publisher = RecordingRealtimePublisher()
	projector = EventRoomProjector(publisher)
	projector.register(bus)

	bus.emit(
		events.ATTENDANCE_RECORDED,
		payload={"gathering": GATHERING, "member": "member-99", "branch": "branch-a"},
	)

	assert shard_channel(GATHERING, shard_for("member-99")) in publisher.rooms(RT_ATTENDANCE_PRESENCE)
	assert broadcast_channel(GATHERING) in publisher.rooms(RT_ATTENDANCE_COUNT)


def test_module_level_emit_unblocks_flo15_reporting_contract():
	"""FLO-15 reporting calls ``emit(name, payload=...)`` — that surface exists."""
	# Swap the module-level sink so the call is side-effect-free and observable.
	events.install_sink(RecordingEventSink())
	sink = events._bus._sink  # type: ignore[attr-defined]
	try:
		result = events.emit(
			events.ATTENDANCE_BULK_RECORDED,
			payload={"gathering": GATHERING, "count": 3, "batch_id": "b", "branch": "branch-a"},
		)
		assert result.name == events.ATTENDANCE_BULK_RECORDED
		assert sink.published  # type: ignore[attr-defined]
		assert sink.published[0][0].payload["count"] == 3  # type: ignore[attr-defined]
	finally:
		events.install_sink(events.NullEventSink())


def test_module_level_projector_singleton_is_consistent():
	assert get_projector() is get_projector()
	assert events.ATTENDANCE_BULK_RECORDED in get_projector().routed_events
