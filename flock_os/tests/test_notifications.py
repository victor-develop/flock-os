"""
Project-level tests for the P2.5 Admin → leader scoped push fan-out service
(FLO-57 / FLO-8 §6.1, ADR-0001 §6).

These run under plain ``pytest`` (no Frappe site / Redis / bench), mirroring the
SQL-light hexagonal pattern from ``test_announcement.py`` / ``test_realtime_fanout.py``.
They pin the FLO-57 Definition of Done:

* **Scoped delivery, no cross-branch leakage** — a branch-subtree push resolves
  leaders only within ``compute_branch_subtree(branch)``; a group-subtree push
  only within the group's subtree. Siblings / parents are never recipients and
  never published to (Phase-1 perms honored).
* **Reuses FLO-14** — the fan-out publishes ``RT_NOTIFICATION`` through the same
  :class:`RealtimePublisher` port + ``BROADCAST_SEGMENT`` channel convention the
  FLO-14 projector owns; no second fan-out mechanism.
* **Emits ``notification.sent`` through the canonical emitter** — exactly one
  ``flock.notification.sent`` flows via :func:`flock_os.events.emit`.
* **Scope validation** — tenant floor, group-branch-binding (ADR §4), and
  missing-anchor rules raise :class:`FlockNotificationError`.
"""

from __future__ import annotations

import pytest

import flock_os.events as events
from flock_os.events import EventBus, RecordingEventSink
from flock_os.notifications import (
	BRANCH_AXIS,
	BROADCAST_SEGMENT,
	DEFAULT_AUDIENCE_ROLE,
	GROUP_AXIS,
	FlockNotificationError,
	NotificationScope,
	ScopedNotificationService,
	branch_notification_room,
	fanout_scoped_notification,
	group_notification_room,
	resolve_scoped_audience,
	rooms_for,
)
from flock_os.realtime import RT_NOTIFICATION, RecordingRealtimePublisher

ORG = "ORG"

# --------------------------------------------------------------------------- #
# In-memory world — two-branch org tree + branch-bound groups (ADR §4).
#
#   HQ ─── North ─── North-A      (groups: G-North-A in North-A, led by M1)
#          South                  (groups: G-South  in South,  led by M2)
#
# Plus a nested group subtree under G-North-A to exercise the group axis:
#   G-North-A (leader M1) ─── G-North-A-Child (leader M3)
# --------------------------------------------------------------------------- #


class RecordingNotificationFanoutGateway:
	"""In-memory fan-out gateway over a known two-tree world."""

	def __init__(self) -> None:
		self.branch_org: dict[str, str] = {
			"HQ": ORG,
			"North": ORG,
			"North-A": ORG,
			"South": ORG,
		}
		self.group_to_branch: dict[str, str] = {
			"G-North-A": "North-A",
			"G-North-A-Child": "North-A",
			"G-South": "South",
		}
		# leader membership per branch / group (accountable leader + roster).
		self.branch_leaders: dict[str, list[str]] = {
			"HQ": [],
			"North": [],
			"North-A": ["M1"],  # G-North-A's leader
			"South": ["M2"],  # G-South's leader — isolation target
		}
		self.group_leaders: dict[str, list[str]] = {
			"G-North-A": ["M1"],
			"G-North-A-Child": ["M3"],  # nested child group leader
			"G-South": ["M2"],
		}

	def branch_exists(self, branch, organization):  # type: ignore[no-untyped-def]
		return self.branch_org.get(branch) == organization

	def group_exists(self, group):  # type: ignore[no-untyped-def]
		return group in self.group_to_branch

	def group_branch(self, group):  # type: ignore[no-untyped-def]
		return self.group_to_branch.get(group)

	def branch_parent_of(self):
		return {"HQ": None, "North": "HQ", "North-A": "North", "South": "HQ"}

	def branch_children_of(self):
		parent_of = self.branch_parent_of()
		children = {name: [] for name in parent_of}
		for name, parent in parent_of.items():
			if parent and parent in children:
				children[parent].append(name)
		return children

	def group_children_of(self):
		return {"G-North-A": ["G-North-A-Child"], "G-North-A-Child": [], "G-South": []}

	def leaders_in_branches(self, branches):  # type: ignore[no-untyped-def]
		out: list[str] = []
		for b in branches:
			out.extend(self.branch_leaders.get(b, []))
		seen = dict.fromkeys(out)
		return tuple(seen)

	def leaders_in_groups(self, groups):  # type: ignore[no-untyped-def]
		out: list[str] = []
		for g in groups:
			out.extend(self.group_leaders.get(g, []))
		seen = dict.fromkeys(out)
		return tuple(seen)


@pytest.fixture()
def gw() -> RecordingNotificationFanoutGateway:
	return RecordingNotificationFanoutGateway()


# --------------------------------------------------------------------------- #
# Channel naming — the FLO-14 broadcast convention re-applied on the node axis.
# --------------------------------------------------------------------------- #


def test_branch_notification_room_uses_broadcast_segment():
	assert branch_notification_room("North-A") == f"flock_os:notify:branch:North-A:{BROADCAST_SEGMENT}"


def test_group_notification_room_uses_broadcast_segment():
	assert group_notification_room("G-North-A") == f"flock_os:notify:group:G-North-A:{BROADCAST_SEGMENT}"


# --------------------------------------------------------------------------- #
# DoD: scoped delivery — no cross-branch leakage (Phase-1 perms honored).
# --------------------------------------------------------------------------- #


def test_branch_scope_audience_includes_branch_and_subtree_only(gw):
	# North's subtree = North + North-A (NOT HQ the parent, NOT South the sibling).
	audience = resolve_scoped_audience(scope=NotificationScope(organization=ORG, branch="North"), gateway=gw)
	assert audience.axis == BRANCH_AXIS
	assert set(audience.nodes) == {"North", "North-A"}
	# Only leaders inside the subtree are recipients.
	assert set(audience.leaders) == {"M1"}
	assert "M2" not in audience.leaders  # South's leader — no leakage.


def test_branch_scope_excludes_parent_and_sibling(gw):
	audience = resolve_scoped_audience(scope=NotificationScope(organization=ORG, branch="North"), gateway=gw)
	assert "HQ" not in audience.nodes  # parent is not a recipient node.
	assert "South" not in audience.nodes  # sibling branch is isolated.


def test_branch_scope_leaf_is_just_itself(gw):
	audience = resolve_scoped_audience(
		scope=NotificationScope(organization=ORG, branch="North-A"), gateway=gw
	)
	assert audience.nodes == ("North-A",)
	assert set(audience.leaders) == {"M1"}


def test_group_scope_audience_includes_group_subtree_only(gw):
	# G-North-A's subtree = G-North-A + G-North-A-Child (NOT G-South).
	audience = resolve_scoped_audience(
		scope=NotificationScope(organization=ORG, group="G-North-A"), gateway=gw
	)
	assert audience.axis == GROUP_AXIS
	assert set(audience.nodes) == {"G-North-A", "G-North-A-Child"}
	# Both subtree leaders are recipients; South's leader is not.
	assert set(audience.leaders) == {"M1", "M3"}
	assert "M2" not in audience.leaders


def test_group_scope_does_not_leak_to_sibling_group(gw):
	audience = resolve_scoped_audience(
		scope=NotificationScope(organization=ORG, group="G-North-A"), gateway=gw
	)
	assert "G-South" not in audience.nodes


def test_rooms_match_resolved_nodes_only(gw):
	# The shard rooms published to are exactly the audience nodes — nothing more.
	audience = resolve_scoped_audience(scope=NotificationScope(organization=ORG, branch="North"), gateway=gw)
	rooms = rooms_for(audience)
	assert set(rooms) == {branch_notification_room("North"), branch_notification_room("North-A")}


# --------------------------------------------------------------------------- #
# DoD: reuses FLO-14 — publishes RT_NOTIFICATION via the sanctioned publisher.
# --------------------------------------------------------------------------- #


def test_fanout_publishes_rt_notification_to_scoped_rooms_via_flo14_publisher(gw):
	publisher = RecordingRealtimePublisher()
	bus = EventBus(sink=RecordingEventSink())
	service = ScopedNotificationService(gw, publisher, bus)

	result = service.fanout(
		scope=NotificationScope(organization=ORG, branch="North"),
		subject="Sunday Service",
		body="See you at 10.",
	)

	# Every publish went through the FLO-14 publisher port on the RT event name.
	assert {c["event"] for c in publisher.calls} == {RT_NOTIFICATION}
	# Exactly the scoped shard rooms — no sibling (South) room, no parent (HQ).
	assert set(publisher.rooms()) == {
		branch_notification_room("North"),
		branch_notification_room("North-A"),
	}
	assert branch_notification_room("South") not in publisher.rooms()
	# The FLO-14 broadcast segment convention is reused.
	assert all(f":{BROADCAST_SEGMENT}" in r for r in publisher.rooms())
	# Audience size forwarded into the realtime payload.
	assert all(c["message"]["audience_size"] == result.recipient_count for c in publisher.calls)
	assert result.recipient_count == 1  # M1 only.


def test_group_fanout_publishes_to_group_rooms(gw):
	publisher = RecordingRealtimePublisher()
	result = fanout_scoped_notification(
		scope=NotificationScope(organization=ORG, group="G-North-A"),
		subject="Huddle",
		gateway=gw,
		publisher=publisher,
		bus=EventBus(sink=RecordingEventSink()),
	)
	assert set(result.rooms) == {
		group_notification_room("G-North-A"),
		group_notification_room("G-North-A-Child"),
	}
	assert set(publisher.rooms()) == set(result.rooms)


def test_publisher_failure_does_not_propagate(gw):
	# Realtime is best-effort (FLO-10 §5.3): a publish failure never fails the push.
	class BoomPublisher:
		def publish(self, *, event, room, message):  # noqa: ARG002
			raise RuntimeError("redis is gone")

	service = ScopedNotificationService(gw, BoomPublisher(), EventBus(sink=RecordingEventSink()))  # type: ignore[arg-type]
	result = service.fanout(scope=NotificationScope(organization=ORG, branch="North"), subject="x")
	# The canonical event is still emitted even though realtime blew up.
	assert result.event.name == events.NOTIFICATION_SENT


def test_empty_audience_emits_event_but_publishes_no_rooms(gw):
	# North has no leaders in its own row (only North-A does) → leaf-free. Use a
	# scope whose subtree has zero leaders to prove the no-op path is clean.
	gw.branch_leaders["North-A"] = []  # strip the only leader
	publisher = RecordingRealtimePublisher()
	result = fanout_scoped_notification(
		scope=NotificationScope(organization=ORG, branch="North-A"),
		subject="x",
		gateway=gw,
		publisher=publisher,
		bus=EventBus(sink=RecordingEventSink()),
	)
	# No realtime publish (no rooms worth fanning to) but the event is recorded.
	assert publisher.calls == []
	assert result.event.name == events.NOTIFICATION_SENT
	assert result.recipient_count == 0


# --------------------------------------------------------------------------- #
# DoD: emits notification.sent through the canonical emitter.
# --------------------------------------------------------------------------- #


def test_fanout_emits_exactly_one_canonical_notification_sent(gw):
	sink = RecordingEventSink()
	bus = EventBus(sink=sink)
	fanout_scoped_notification(
		scope=NotificationScope(organization=ORG, branch="North"),
		subject="Reminder",
		body="10 AM",
		gateway=gw,
		publisher=RecordingRealtimePublisher(),
		bus=bus,
	)
	sent = [e for e, _rt, _room in sink.published if e.name == events.NOTIFICATION_SENT]
	assert len(sent) == 1
	event = sent[0]
	# Scope anchors flow through the canonical event for subscribers + audit.
	assert event.scope["organization"] == ORG
	assert event.scope["branch"] == "North"
	assert event.payload["subject"] == "Reminder"
	assert event.payload["audience_size"] == 1
	assert event.payload["axis"] == BRANCH_AXIS


def test_canonical_event_carrys_notification_ref_and_category(gw):
	sink = RecordingEventSink()
	bus = EventBus(sink=sink)
	fanout_scoped_notification(
		scope=NotificationScope(organization=ORG, branch="North"),
		subject="Urgent",
		gateway=gw,
		publisher=RecordingRealtimePublisher(),
		bus=bus,
		category="Urgent",
		notification_ref="Flock Announcement/ANN-1",
	)
	event = [e for e, _r, _rm in sink.published if e.name == events.NOTIFICATION_SENT][0]
	assert event.payload["category"] == "Urgent"
	assert event.payload["notification_ref"] == "Flock Announcement/ANN-1"
	assert event.payload["audience_role"] == DEFAULT_AUDIENCE_ROLE


# --------------------------------------------------------------------------- #
# Scope validation — tenant floor + group-branch-binding (ADR §4) + anchors.
# --------------------------------------------------------------------------- #


def test_missing_organization_rejected(gw):
	with pytest.raises(FlockNotificationError, match="organization is required"):
		fanout_scoped_notification(
			scope=NotificationScope(organization="", branch="North"),
			subject="x",
			gateway=gw,
			publisher=RecordingRealtimePublisher(),
			bus=EventBus(),
		)


def test_missing_anchor_rejected(gw):
	with pytest.raises(FlockNotificationError, match="branch or group anchor"):
		fanout_scoped_notification(
			scope=NotificationScope(organization=ORG),
			subject="x",
			gateway=gw,
			publisher=RecordingRealtimePublisher(),
			bus=EventBus(),
		)


def test_branch_not_in_organization_rejected(gw):
	with pytest.raises(FlockNotificationError, match="not a member of organization"):
		resolve_scoped_audience(scope=NotificationScope(organization="OTHER-ORG", branch="North"), gateway=gw)


def test_unknown_branch_rejected(gw):
	with pytest.raises(FlockNotificationError, match="not a member of organization"):
		resolve_scoped_audience(scope=NotificationScope(organization=ORG, branch="Ghost"), gateway=gw)


def test_unknown_group_rejected(gw):
	with pytest.raises(FlockNotificationError, match="does not exist"):
		resolve_scoped_audience(scope=NotificationScope(organization=ORG, group="Ghost"), gateway=gw)


def test_group_branch_binding_violation_rejected(gw):
	# ADR §4: G-South belongs to South — a push scoped to branch North + G-South
	# must be rejected (the group is not branch-bound to the scope's branch).
	with pytest.raises(FlockNotificationError, match="group-branch-binding"):
		resolve_scoped_audience(
			scope=NotificationScope(organization=ORG, branch="North", group="G-South"), gateway=gw
		)


def test_group_in_other_organization_rejected(gw):
	# Tenant floor: G-North-A's branch (North-A) is in ORG, not OTHER-ORG.
	with pytest.raises(FlockNotificationError, match="not a member of organization"):
		resolve_scoped_audience(
			scope=NotificationScope(organization="OTHER-ORG", group="G-North-A"), gateway=gw
		)


# --------------------------------------------------------------------------- #
# Scope model + service plumbing.
# --------------------------------------------------------------------------- #


def test_scope_axis_resolves_by_anchor():
	assert NotificationScope(organization=ORG, branch="North").axis == BRANCH_AXIS
	assert NotificationScope(organization=ORG, group="G-North-A").axis == GROUP_AXIS
	# group wins when both anchors are set (the tighter axis drives the fan-out).
	assert NotificationScope(organization=ORG, branch="North", group="G-North-A").axis == GROUP_AXIS
	assert NotificationScope(organization=ORG).axis == ""


def test_group_with_no_branch_binding_rejected(gw):
	# ADR §4: a group that exists but carries no branch binding can't be scoped.
	gw.group_to_branch["G-Orphan"] = None
	gw.group_to_branch.pop("G-Orphan", None)

	class OrphanGW(RecordingNotificationFanoutGateway):
		def group_exists(self, group):  # type: ignore[no-untyped-def]
			return group == "G-Orphan" or super().group_exists(group)

		def group_branch(self, group):  # type: ignore[no-untyped-def]
			if group == "G-Orphan":
				return None
			return super().group_branch(group)

	with pytest.raises(FlockNotificationError, match="no branch binding"):
		resolve_scoped_audience(
			scope=NotificationScope(organization=ORG, group="G-Orphan"), gateway=OrphanGW()
		)


def test_install_publisher_swaps_publisher(gw):
	from flock_os.notifications import ScopedNotificationService

	service = ScopedNotificationService(gw)
	first = service._publisher  # type: ignore[attr-defined]
	rec = RecordingRealtimePublisher()
	service.install_publisher(rec)
	assert service._publisher is rec  # type: ignore[attr-defined]
	assert service._publisher is not first  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- #
# Gateway protocol + module-level accessor (hexagonal plumbing).
# --------------------------------------------------------------------------- #


def test_gateway_protocol_is_runtime_checkable(gw):
	from flock_os.notifications import NotificationFanoutGateway

	assert isinstance(gw, NotificationFanoutGateway)


def test_install_and_get_service(gw):
	from flock_os import notifications

	service = notifications.install_gateway(gw)
	assert service is notifications.get_service()
	# Restore the default to keep module-level state clean for other tests.
	notifications.install_gateway(notifications.NullNotificationFanoutGateway())


# --------------------------------------------------------------------------- #
# FLO-465 §1: FrappeNotificationFanoutGateway._leaders must filter status='Active'.
#
# Inactive leaders (e.g. elders emeriti, removed officers whose roster row is
# kept for history) must NOT receive scoped notifications. The roster read also
# has to match the (branch, status) / (group, status) composites (FLO-459) so
# the fan-out path is index-served.
# --------------------------------------------------------------------------- #


class _FakeFrappeDB:
	"""In-memory Flock Group / Group Member table over a tiny world.

	Simulates just enough of ``frappe.get_all(filters=..., pluck=...)`` for the
	``_leaders`` read path: applies the dict filters as equality / `["in", ...]`
	predicates and returns the plucked column. Behavior-grounded (the test
	asserts an Inactive leader is actually excluded), not just filter-shape.

	Every ``get_all`` call is recorded in ``self.calls`` so tests can assert on
	the exact (doctype, filters) pair the gateway issued.
	"""

	def __init__(self) -> None:
		# Flock Group rows: name -> {branch, leader}.
		self.groups: dict[str, dict[str, str | None]] = {
			"G-North-A": {"branch": "North-A", "leader": "M1"},
			"G-South": {"branch": "South", "leader": "M2"},
		}
		# Flock Group Member rows.
		self.group_members: list[dict[str, str]] = [
			# Active roster leader in North-A — must be included.
			{"member": "M1", "role": "Leader", "status": "Active", "branch": "North-A", "group": "G-North-A"},
			# Inactive roster leader in North-A — must be EXCLUDED.
			{
				"member": "M9-Inactive",
				"role": "Leader",
				"status": "Inactive",
				"branch": "North-A",
				"group": "G-North-A",
			},
			# Active Co-Leader in South — isolation target (different branch).
			{"member": "M2", "role": "Co-Leader", "status": "Active", "branch": "South", "group": "G-South"},
		]
		# Captured (doctype, filters) for each get_all call.
		self.calls: list[dict] = []

	def get_all(self, doctype, *, filters=None, pluck=None, **_kwargs):  # type: ignore[no-untyped-def]
		filters = filters or {}
		self.calls.append({"doctype": doctype, "filters": dict(filters)})
		if doctype == "Flock Group":
			rows = [
				{"name": name, **cols}
				for name, cols in self.groups.items()
				if all(self._match({"name": name, **cols}, k, v) for k, v in filters.items())
			]
		elif doctype == "Flock Group Member":
			rows = [
				row for row in self.group_members if all(self._match(row, k, v) for k, v in filters.items())
			]
		else:  # pragma: no cover - the gateway only reads the two doctypes above.
			rows = []
		if pluck:
			return [r[pluck] for r in rows if r.get(pluck)]
		return rows

	@staticmethod
	def _match(row, key, value) -> bool:  # type: ignore[no-untyped-def]
		if isinstance(value, list) and len(value) == 2 and value[0] == "in":
			return row.get(key) in value[1]
		return row.get(key) == value


class _FakeFrappe:
	def __init__(self) -> None:
		self.db = _FakeFrappeDB()
		self.get_all = self.db.get_all

	@property
	def calls(self) -> list[dict]:
		return self.db.calls


def test_leaders_roster_read_filters_status_active(monkeypatch) -> None:
	"""The roster ``frappe.get_all`` call issued by ``_leaders`` MUST carry
	``status == 'Active'``. This is the contract that keeps Inactive leaders off
	the notification audience and aligns the read with the FLO-459 composites."""
	from flock_os.notifications import FrappeNotificationFanoutGateway

	fake = _FakeFrappe()
	monkeypatch.setattr(FrappeNotificationFanoutGateway, "_frappe", fake)

	gateway = FrappeNotificationFanoutGateway()
	gateway.leaders_in_branches(["North-A"])

	# Find the Flock Group Member read (the roster read — the one that used to
	# omit status). The Flock Group accountable-leader read is separate.
	roster_calls = [call for call in fake.calls if call["doctype"] == "Flock Group Member"]
	assert roster_calls, "expected a Flock Group Member roster read"
	assert roster_calls[-1]["filters"].get("status") == "Active", (
		"roster_filters must include status='Active' so Inactive leaders are excluded (FLO-465 §1)"
	)


def test_leaders_excludes_inactive_roster_leader(monkeypatch) -> None:
	"""Behavioral guarantee: an Inactive Leader on the roster is NOT part of the
	resolved notification audience. Pins the P1 correctness fix end-to-end."""
	from flock_os.notifications import FrappeNotificationFanoutGateway

	fake = _FakeFrappe()
	monkeypatch.setattr(FrappeNotificationFanoutGateway, "_frappe", fake)

	gateway = FrappeNotificationFanoutGateway()
	audience = gateway.leaders_in_branches(["North-A"])

	assert "M1" in audience  # Active Leader still resolved.
	assert "M9-Inactive" not in audience, "Inactive leader must not receive notifications (FLO-465 §1)"


def test_leaders_excludes_inactive_roster_leader_group_axis(monkeypatch) -> None:
	"""Same invariant on the group axis — the group-scoped fan-out path also
	excludes Inactive roster leaders."""
	from flock_os.notifications import FrappeNotificationFanoutGateway

	fake = _FakeFrappe()
	monkeypatch.setattr(FrappeNotificationFanoutGateway, "_frappe", fake)

	gateway = FrappeNotificationFanoutGateway()
	audience = gateway.leaders_in_groups(["G-North-A"])

	assert "M1" in audience
	assert "M9-Inactive" not in audience
