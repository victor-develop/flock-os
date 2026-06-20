"""
Realtime engagement tier authorization edge-case regressions — FLO-428.

These pin the four authorization edge cases the 15k burst exposes on the
realtime engagement tier (FLO-428 audit). The audit found **no genuine bugs**
— every path is safe by design — so this file exists to *freeze* the safe
behavior so a future refactor cannot silently regress it. Each section names the
edge case, the observed behavior, and the test that proves it; the companion
narrative lives in ``docs/operations/realtime-edge-cases.md``.

Scope split (FLO-428 §scope):

* Pure-Python decision surfaces are pinned HERE under plain ``pytest`` (no
  bench, no Redis, no socket.io) — the projector's fan-out shape, the
  per-join scope gate, and the branch-scope decision.
* Node-side behavior (auth-cache TTL vs per-join scope check, Socket.IO room
  set semantics under a reconnect storm) is pinned by the regression tests
  added to ``realtime/handlers/flock_room_handlers.test.mjs`` and
  ``realtime/middlewares/flock_auth_cache.test.mjs`` (the existing
  one-test-file-per-module convention).

The four edge cases (matched 1:1 to the test classes below):

1. **Expired / revoked ticket on connect/join** —
   :class:`TestRevokedTicketOnJoin` pins that the scope gate consults only the
   user's *current* roles + allowed-branch set, never any ticket/session cache,
   so revocation takes effect on the very next ``join`` decision (and on
   reconnect once the underlying session is rejected by Frappe).
2. **Room capacity mid-broadcast** —
   :class:`TestRoomCapacityMidBroadcast` pins that the projector's fan-out
   shape is fixed by the shard count, NOT by any capacity / attendee-count
   guard (there is none), and that a publish failure on one target never
   cascades to the others (§5.3 best-effort).
3. **Conflicting multi-branch group permissions** —
   :class:`TestMultiBranchPermissionDeterminism` pins that a user in multiple
   branch groups gets a deterministic allow/deny per gathering (the
   materialized allowed set is the single source of truth; no group-tree
   traversal at decision time, no nondeterministic "which scope wins").
4. **Reconnect storm idempotency** —
   :class:`TestReconnectStormProjectorStability` pins that the projector holds
   no per-room / per-event mutable state, so re-emitting the same domain event
   N times during a reconnect storm produces the SAME emission plan every time
   (no drift, no leak). The Socket.IO room-set dedup is pinned on the Node side.
"""

from __future__ import annotations

import inspect

import flock_os.events as events
from flock_os import permissions
from flock_os.events import DomainEvent, EventBus, RecordingEventSink
from flock_os.realtime import (
	DEFAULT_SHARD_COUNT,
	EventRoomProjector,
	RecordingRealtimePublisher,
	_MappingEventBranchResolver,
	all_shards,
	broadcast_channel,
	event_room_join_allowed,
	shard_channel,
	shard_for,
	shards_for,
)

# --------------------------------------------------------------------------- #
# Shared stubs (mirror test_realtime_room_scope.py so the audit reads the same
# shape the existing gate does — no second vocabulary to learn).
# --------------------------------------------------------------------------- #


class _StubGateway:
	"""Minimal permission gateway whose state can be mutated between calls.

	The audit needs to flip a user's allowed-set / roles mid-scenario (simulate
	a revoke) and assert the next ``event_room_join_allowed`` call reflects the
	new state immediately. The immutable variant in
	``test_realtime_room_scope.py`` does not support that, so this stub exposes
	setters.
	"""

	def __init__(self, *, roles=(), allowed=()) -> None:
		self.set_state(roles=roles, allowed=allowed)

	def set_state(self, *, roles=(), allowed=()) -> None:
		self._roles = frozenset(roles)
		self._allowed = tuple(allowed)

	def get_user_roles(self, user: str) -> frozenset[str]:  # noqa: ARG002
		return self._roles

	def list_branch_user_permissions(self, user: str) -> tuple[str, ...]:  # noqa: ARG002
		return self._allowed


def _resolver(branches: dict[str, str]) -> _MappingEventBranchResolver:
	return _MappingEventBranchResolver(branches)


GATHERING_A = "g-15k-a"
GATHERING_B = "g-15k-b"


# =========================================================================== #
# Edge case 1 — Expired / revoked ticket on connect/join
# =========================================================================== #
class TestRevokedTicketOnJoin:
	"""The scope gate has NO ticket/session cache input — revocation is live.

	Edge case (FLO-428 §1): a device holds a signed ticket that was valid at
	issue but revoked before the burst. Audit verdict: ``event_room_join_allowed``
	takes ``room, user, gateway, resolver`` only; there is no ticket / sid /
	session parameter. The decision reflects whatever the gateway reports
	*right now*, so the moment a revoke lands in the permission store the next
	``join`` decision denies — no TTL window on the Python side. The auth cache
	(``realtime/middlewares/flock_auth_cache.js``) is a *connect-time* Socket.IO
	middleware cache only; it does not bypass this per-join scope gate (the Node
	regressions added alongside this file pin that independence).
	"""

	gathering_branches = {GATHERING_A: "branch-a"}

	def test_signature_has_no_ticket_or_session_parameter(self):
		"""The gate cannot stale-trust a ticket because it has no such input."""
		params = inspect.signature(event_room_join_allowed).parameters
		param_names = set(params)
		# Forbidden inputs — their absence IS the safety property.
		assert "ticket" not in param_names
		assert "sid" not in param_names
		assert "session" not in param_names
		assert "cache" not in param_names
		# Required inputs — the live permission state.
		assert param_names == {"room", "user", "gateway", "resolver"}

	def test_revoked_allowed_set_denies_the_next_join_immediately(self):
		"""A user whose allowed-set is cleared between two joins gets deny on
		the very next call — no lingering 'previously allowed' state."""
		gw = _StubGateway(roles=[permissions.ROLE_BRANCH_ADMIN], allowed=["branch-a"])
		kwargs = dict(
			room=f"flock_os:event:{GATHERING_A}:broadcast",
			user="leader@flock.os",
			gateway=gw,
			resolver=_resolver(self.gathering_branches),
		)
		# Before revoke: allowed.
		assert event_room_join_allowed(**kwargs)
		# Revoke: empty the allowed set (mirrors a Frappe User Permission delete).
		gw.set_state(roles=[permissions.ROLE_BRANCH_ADMIN], allowed=[])
		# Next decision is deny — no TTL, no cache, no carryover.
		assert not event_room_join_allowed(**kwargs)

	def test_revoked_role_demotion_denies_the_next_join_immediately(self):
		"""Stripping the Branch Admin role (kept allowed-set) also denies live."""
		gw = _StubGateway(roles=[permissions.ROLE_BRANCH_ADMIN], allowed=["branch-a"])
		kwargs = dict(
			room=f"flock_os:event:{GATHERING_A}:shard:3",
			user="leader@flock.os",
			gateway=gw,
			resolver=_resolver(self.gathering_branches),
		)
		assert event_room_join_allowed(**kwargs)
		# Demote to a group-scoped role with no global branch bypass.
		gw.set_state(roles=[permissions.ROLE_GROUP_LEADER], allowed=[])
		assert not event_room_join_allowed(**kwargs)

	def test_re_granting_scope_re_allows_the_next_join_immediately(self):
		"""Inverse direction: re-issuing the ticket re-allows on the next call.

		Pins that the gate's reading is fully live in BOTH directions — there is
		no negative cache either (a previously-denied user is not stuck denied
		once their scope is restored).
		"""
		gw = _StubGateway(roles=[permissions.ROLE_BRANCH_ADMIN], allowed=[])
		kwargs = dict(
			room=f"flock_os:event:{GATHERING_A}:broadcast",
			user="leader@flock.os",
			gateway=gw,
			resolver=_resolver(self.gathering_branches),
		)
		assert not event_room_join_allowed(**kwargs)
		gw.set_state(roles=[permissions.ROLE_BRANCH_ADMIN], allowed=["branch-a"])
		assert event_room_join_allowed(**kwargs)


# =========================================================================== #
# Edge case 2 — Room capacity mid-broadcast
# =========================================================================== #
class TestRoomCapacityMidBroadcast:
	"""No capacity cap exists; sharding IS the capacity strategy (FLO-428 §2).

	Audit verdict: ``realtime.py`` has no ``max_*`` / ``capacity`` / ``cap`` /
	``overflow`` guard anywhere on the broadcast path. The projector's fan-out
	shape is a pure function of the shard count + the event name — never of the
	room population. So a "room near capacity mid-broadcast" cannot trip a guard
	and cascade: there is no guard to trip. The shard design (N≈10, ~1.5k
	subscribers/shard at 15k) keeps every individual publish cheap, and §5.3
	best-effort swallows any single publish failure without cascading.
	"""

	def test_bulk_attendance_emit_count_is_shard_count_plus_one_regardless_of_size(self):
		"""A 15k-attendee bulk event emits exactly N+1 publishes (N shards + 1
		broadcast). Same shape as a 10-attendee bulk event — capacity plays no
		role in routing."""
		for count in (10, 1_500, 15_000):
			publisher = RecordingRealtimePublisher()
			projector = EventRoomProjector(publisher)
			projector.project(
				DomainEvent(
					events.ATTENDANCE_BULK_RECORDED,
					{
						"gathering": GATHERING_A,
						"count": count,
						"batch_id": f"b-{count}",
						"branch": "branch-a",
					},
				)
			)
			# N shard publishes + 1 broadcast publish, independent of `count`.
			assert len(publisher.calls) == DEFAULT_SHARD_COUNT + 1, (
				f"capacity-shaped routing would change this with count={count}"
			)

	def test_game_close_fans_all_shards_and_broadcast_without_attendee_list(self):
		"""A live-game close carries no attendee list — fans every shard
		unchanged. Pins that the close path has no early-exit on audience size."""
		publisher = RecordingRealtimePublisher()
		projector = EventRoomProjector(publisher)
		emissions = projector.project(
			DomainEvent(
				events.ENGAGEMENT_SESSION_CLOSED,
				{"gathering": GATHERING_A, "session": "game-15k", "branch": "branch-a"},
			)
		)
		expected_rooms = {shard_channel(GATHERING_A, s) for s in all_shards()} | {
			broadcast_channel(GATHERING_A)
		}
		assert {e.room for e in emissions} == expected_rooms
		assert len(emissions) == DEFAULT_SHARD_COUNT + 1

	def test_single_attendance_always_emits_exactly_two_targets(self):
		"""Single-record presence is always attendee-shard + broadcast — never
		skipped, regardless of how full the shard already is (no cap concept)."""
		publisher = RecordingRealtimePublisher()
		projector = EventRoomProjector(publisher)
		# Project 15k distinct single-attendance events in the same shard.
		target_shard = 0
		members_in_shard = [f"m-{i}" for i in range(20_000) if shard_for(f"m-{i}") == target_shard]
		assert len(members_in_shard) > 1_000, "fixture: must overfill one shard"
		for member in members_in_shard:
			projector.project(
				DomainEvent(
					events.ATTENDANCE_RECORDED,
					{"gathering": GATHERING_A, "member": member, "branch": "branch-a"},
				)
			)
		# Every emit produced exactly 2 publishes (presence shard + broadcast).
		# No emit was ever dropped or reshaped by the shard's population.
		expected_presence = {shard_channel(GATHERING_A, shard_for(m)) for m in members_in_shard}
		actual_presence = {c["room"] for c in publisher.calls if c["event"] == "flock_os:attendance:presence"}
		assert actual_presence == expected_presence
		# 2 publishes per single-attendance event, invariant.
		assert len(publisher.calls) == 2 * len(members_in_shard)

	def test_publish_failure_on_one_target_does_not_cascade(self):
		"""§5.3: realtime is best-effort. A publish failure (the closest thing
		to an 'overflow') is swallowed per-target and never propagates to other
		shards or back to the emitter."""

		class OneBoomPublisher:
			"""Fails the first publish, succeeds on the rest, records all."""

			def __init__(self) -> None:
				self.calls = 0
				self.ok_calls: list[str] = []

			def publish(self, *, event, room, message):  # noqa: ARG002
				self.calls += 1
				if self.calls == 1:
					raise RuntimeError("redis publish timeout (capacity burst)")
				self.ok_calls.append(room)

		publisher = OneBoomPublisher()
		projector = EventRoomProjector(publisher)  # type: ignore[arg-type]
		# Bulk event → N+1 publishes; the first raises, the rest must still fire.
		emissions = projector.project(
			DomainEvent(
				events.ATTENDANCE_BULK_RECORDED,
				{
					"gathering": GATHERING_A,
					"count": 15_000,
					"batch_id": "b-15k",
					"branch": "branch-a",
				},
			)
		)
		# Routing still produced the full plan — the audit surface is intact.
		assert len(emissions) == DEFAULT_SHARD_COUNT + 1
		# Every OTHER publish still happened (no cascade, no early-abort).
		assert len(publisher.ok_calls) == DEFAULT_SHARD_COUNT
		# And the projector call returned normally (no exception escaped).
		assert publisher.calls == DEFAULT_SHARD_COUNT + 1


# =========================================================================== #
# Edge case 3 — Conflicting multi-branch group permissions
# =========================================================================== #
class TestMultiBranchPermissionDeterminism:
	"""Per-gathering decision; allowed-set union is the single source of truth.

	Edge case (FLO-428 §3): a member in two branch groups with different
	event-room scopes. Audit verdict: a gathering is branch-bound (exactly one
	``Flock Gathering.branch``, ADR §4.2), so the gate makes a single
	allow/deny per gathering — there is no "two scopes at once" to conflict.
	The user's materialized allowed-set is the union of their group subtrees,
	so the decision is deterministic and order-independent. Whichever group
	grants the gathering's branch, the user joins; whichever group doesn't, no
	shadow-deny overrides.
	"""

	gathering_branches = {
		"g-north": "branch-north",
		"g-south": "branch-south",
		"g-east": "branch-east",
	}

	def test_user_in_two_branch_groups_joins_both_gatherings(self):
		"""A user whose allowed-set is the union of two group subtrees may join
		gatherings in either branch — no conflict between the two memberships."""
		gw = _StubGateway(
			roles=[permissions.ROLE_BRANCH_ADMIN],
			allowed=["branch-north", "branch-south"],
		)
		resolver = _resolver(self.gathering_branches)
		assert event_room_join_allowed(
			room="flock_os:event:g-north:broadcast",
			user="dual@flock.os",
			gateway=gw,
			resolver=resolver,
		)
		assert event_room_join_allowed(
			room="flock_os:event:g-south:broadcast",
			user="dual@flock.os",
			gateway=gw,
			resolver=resolver,
		)

	def test_allowed_set_order_does_not_change_the_decision(self):
		"""Set-union is commutative — order of branch permissions is irrelevant.

		Pins the 'which scope wins' question: there is no precedence, because
		the decision reduces to ``branch in allowed_set``, which is order-free.
		"""
		branches = ["branch-north", "branch-south"]
		gw_forward = _StubGateway(roles=[permissions.ROLE_BRANCH_ADMIN], allowed=branches)
		gw_reverse = _StubGateway(roles=[permissions.ROLE_BRANCH_ADMIN], allowed=reversed(branches))
		resolver = _resolver(self.gathering_branches)
		for room in (
			"flock_os:event:g-north:broadcast",
			"flock_os:event:g-south:shard:0",
		):
			assert event_room_join_allowed(
				room=room, user="u", gateway=gw_forward, resolver=resolver
			) == event_room_join_allowed(room=room, user="u", gateway=gw_reverse, resolver=resolver)

	def test_dual_membership_does_not_grant_the_third_branch(self):
		"""Allowed=[north, south] does NOT leak east — no transitive grant via
		group-tree traversal at decision time. The set IS the scope."""
		gw = _StubGateway(
			roles=[permissions.ROLE_BRANCH_ADMIN],
			allowed=["branch-north", "branch-south"],
		)
		resolver = _resolver(self.gathering_branches)
		assert not event_room_join_allowed(
			room="flock_os:event:g-east:broadcast",
			user="dual@flock.os",
			gateway=gw,
			resolver=resolver,
		)
		assert not event_room_join_allowed(
			room="flock_os:event:g-east:shard:5",
			user="dual@flock.os",
			gateway=gw,
			resolver=resolver,
		)

	def test_repeated_calls_are_deterministic(self):
		"""No hidden state: same inputs → same output across 100 calls (pins
		there is no per-call randomization or last-result caching)."""
		gw = _StubGateway(roles=[permissions.ROLE_BRANCH_ADMIN], allowed=["branch-north"])
		resolver = _resolver(self.gathering_branches)
		results = [
			event_room_join_allowed(
				room="flock_os:event:g-north:broadcast",
				user="u",
				gateway=gw,
				resolver=resolver,
			)
			for _ in range(100)
		]
		assert all(r is True for r in results)

	def test_subtree_union_via_materialized_set(self):
		"""A regional admin whose subtree spans north + north.child joins
		gatherings in BOTH branches via the single materialized allowed-set —
		no extra resolver/gateway call per gathering (deterministic, cheap)."""
		gw = _StubGateway(
			roles=[permissions.ROLE_BRANCH_ADMIN],
			allowed=["branch-north", "branch-north-child"],
		)
		resolver = _resolver({"g-child": "branch-north-child", "g-parent": "branch-north"})
		assert event_room_join_allowed(
			room="flock_os:event:g-parent:broadcast",
			user="regional@flock.os",
			gateway=gw,
			resolver=resolver,
		)
		assert event_room_join_allowed(
			room="flock_os:event:g-child:shard:7",
			user="regional@flock.os",
			gateway=gw,
			resolver=resolver,
		)


# =========================================================================== #
# Edge case 4 — Reconnect storm idempotency (Python projector side)
# =========================================================================== #
class TestReconnectStormProjectorStability:
	"""The projector holds no per-room state — re-emit is a pure function.

	Edge case (FLO-428 §4): mass reconnect after a socketio worker restart.
	Audit verdict: the Socket.IO room is a set, so client re-``join`` events
	dedupe at the socket layer (pinned on the Node side in
	``flock_room_handlers.test.mjs``). The Python projector is equally
	idempotent: it owns no per-room / per-event mutable state, so re-emitting
	the same domain event N times (the reconnect-storm shape on the bus)
	produces the SAME emission plan every time. No drift, no leak, no
	growing list of "joined" rooms.
	"""

	def test_repeated_attendance_event_emits_identical_plan_every_time(self):
		"""Projecting the same event 1_000 times yields 1_000 identical emission
		plans — the projector accumulates NO state between calls."""
		projector = EventRoomProjector(RecordingRealtimePublisher())
		event = DomainEvent(
			events.ATTENDANCE_RECORDED,
			{"gathering": GATHERING_A, "member": "member-7", "branch": "branch-a"},
		)
		first = projector.project(event)
		assert len(first) == 2
		for _ in range(999):
			assert projector.project(event) == first

	def test_repeated_bulk_event_touched_shards_do_not_grow(self):
		"""A bulk event with attendee refs always targets the SAME shard set —
		no drift across a reconnect-storm's worth of re-emits."""
		projector = EventRoomProjector(RecordingRealtimePublisher())
		refs = [f"member-{i}" for i in range(250)]
		expected_shards = set(shards_for(refs))
		event = DomainEvent(
			events.ATTENDANCE_BULK_RECORDED,
			{
				"gathering": GATHERING_A,
				"count": len(refs),
				"batch_id": "b-storm",
				"branch": "branch-a",
				"attendees": refs,
			},
		)
		for _ in range(500):
			emissions = projector.project(event)
			actual_shards = {
				int(e.room.rsplit(":", 1)[-1])
				for e in emissions
				if e.realtime_event == "flock_os:attendance:presence"
			}
			assert actual_shards == expected_shards

	def test_subscribed_projector_handles_repeated_bus_emits_without_drift(self):
		"""End-to-end: a reconnect-storm's worth of bus emits reaches the
		subscribed projector and produces the same room set every time — no
		double-join, no growing room list (the projector has no join state)."""
		bus = EventBus(sink=RecordingEventSink())
		publisher = RecordingRealtimePublisher()
		projector = EventRoomProjector(publisher)
		projector.register(bus)

		players = [f"player-{i}" for i in range(60)]
		touched = set(shards_for(players))
		payload = {
			"gathering": GATHERING_A,
			"count": len(players),
			"batch_id": "game-storm-close",
			"branch": "branch-a",
			"attendees": players,
		}

		# Emit the SAME bulk event 100 times — simulates 100 reconnect-storm
		# replays of the same game-close (e.g. a redelivery / retry storm).
		for _ in range(100):
			bus.emit(events.ATTENDANCE_BULK_RECORDED, payload=payload)

		presence_rooms = set(publisher.rooms("flock_os:attendance:presence"))
		# The set of distinct rooms is exactly the touched shards — NOT 100x
		# duplicated, because rooms are channel names (set semantics).
		assert presence_rooms == {shard_channel(GATHERING_A, s) for s in touched}
		# ... but the publisher records every emit (the projector is a pure
		# function: 100 emits → 100 × (touched_shards + 1 broadcast) calls).
		expected_call_count = 100 * (len(touched) + 1)
		assert len(publisher.calls) == expected_call_count
