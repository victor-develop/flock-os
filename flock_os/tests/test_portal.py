"""
Project-level tests for the admin announcement compose-context (FLO-60 / FLO-8 §8).

Run under plain ``pytest`` (no Frappe site / Redis / bench), mirroring the
SQL-light hexagonal pattern from ``test_notifications`` / ``test_scheduling``.
They pin the FLO-60 Definition of Done:

* **No cross-subtree leakage in the UI targeting** — a Branch Admin's compose
  context offers only their materialized subtree (siblings never appear), the
  group picker is confined to the same subtree, and a forged cross-subtree
  target is rejected by ``assert_target_in_context``.
* **Happy-path e2e** — admin picks scope (from the offered context) -> audience
  preview resolves the branch subtree -> publish fans out to exactly the subtree
  shard rooms; sibling leaders never receive it.

The e2e drives the *same* pure primitives the FLO-94 transport
(:func:`flock_os.scheduling.publish_announcement`) orchestrates —
:func:`resolve_audience_branches` + :func:`fanout_scoped_notification` — so the
test exercises the real compose->preview->send chain without a bench.
"""

from __future__ import annotations

import pytest

from flock_os.events import NOTIFICATION_SENT, EventBus, RecordingEventSink
from flock_os.notifications import (
	NotificationScope,
	ScopedNotificationService,
	branch_notification_room,
)
from flock_os.portal import (
	AUDIENCE_ROLE_OPTIONS,
	CATEGORY_OPTIONS,
	CHANNEL_OPTIONS,
	PRIORITY_OPTIONS,
	FlockPortalError,
	assert_target_in_context,
	build_compose_context,
	is_compose_admin,
)
from flock_os.realtime import RT_NOTIFICATION, RecordingRealtimePublisher
from flock_os.scheduling import resolve_audience_branches

ORG = "ORG"

# --------------------------------------------------------------------------- #
# In-memory world — reuses the test_notifications two-tree model so the compose
# layer + the FLO-57 fan-out layer share one consistent view (ADR §4).
#
#   HQ ─── North ─── North-A   (groups: G-North-A, G-North-A-Child — leaders M1/M3)
#          South              (group:   G-South                — leader M2)
#
# alice = Branch Admin scoped to the North subtree (UP rows: North, North-A).
# bob   = Org Admin (global — sees every branch in ORG).
# carol = Group Leader scoped to North-A.
# dave  = Member (no compose role).
# --------------------------------------------------------------------------- #
BRANCH_ORG = {"HQ": ORG, "North": ORG, "North-A": ORG, "South": ORG}
BRANCH_PARENT = {"HQ": None, "North": "HQ", "North-A": "North", "South": "HQ"}
GROUP_TO_BRANCH = {"G-North-A": "North-A", "G-North-A-Child": "North-A", "G-South": "South"}


def _branch_children() -> dict[str, list[str]]:
	children = {name: [] for name in BRANCH_PARENT}
	for name, parent in BRANCH_PARENT.items():
		if parent and parent in children:
			children[parent].append(name)
	return children


class RecordingComposeGateway:
	"""In-memory compose gateway over the two-tree world."""

	def __init__(self) -> None:
		# user -> targetable branch subtree (the materialized UP set; ADR §6.2).
		self.user_branches: dict[str, tuple[str, ...]] = {
			"alice": ("North", "North-A"),  # Branch Admin: North subtree (no HQ parent, no South).
			"carol": ("North-A",),  # Group Leader: her branch only.
		}
		self.user_roles: dict[str, tuple[str, ...]] = {
			"alice": ("Flock Branch Admin",),
			"bob": ("Flock Org Admin",),
			"carol": ("Flock Group Leader",),
			"dave": ("Flock Member",),
		}

	def get_user_roles(self, user):
		return self.user_roles.get(user, ())

	def get_user_organization(self, user):  # noqa: ARG002
		return ORG

	def targetable_branches(self, user):
		if frozenset(self.user_roles.get(user, ())) & {"Flock Org Admin", "Flock Auditor"}:
			# Global roles see every branch in the org.
			return tuple(BRANCH_ORG.keys())
		return self.user_branches.get(user, ())

	def branch_organization(self, branch):
		return BRANCH_ORG.get(branch)

	def branch_label(self, branch):
		return branch  # names are human enough for the test world.

	def groups_for_branches(self, branches):
		out = []
		for g, b in GROUP_TO_BRANCH.items():
			if b in set(branches):
				out.append({"name": g, "branch": b, "label": g})
		out.sort(key=lambda r: (r["branch"], r["name"]))
		return tuple(out)


@pytest.fixture()
def gw() -> RecordingComposeGateway:
	return RecordingComposeGateway()


# --------------------------------------------------------------------------- #
# DoD: no cross-subtree leakage in the UI targeting.
# --------------------------------------------------------------------------- #


def test_branch_admin_offered_only_their_subtree(gw):
	ctx = build_compose_context(user="alice", gateway=gw)
	offered = {b["name"] for b in ctx["branches"]}
	assert offered == {"North", "North-A"}  # the North subtree.
	assert "South" not in offered  # sibling — never offered.
	assert "HQ" not in offered  # parent — never offered.


def test_global_admin_sees_every_branch(gw):
	ctx = build_compose_context(user="bob", gateway=gw)
	offered = {b["name"] for b in ctx["branches"]}
	assert offered == {"HQ", "North", "North-A", "South"}  # whole org.


def test_group_picker_confined_to_targetable_branches(gw):
	ctx = build_compose_context(user="alice", gateway=gw)
	offered_groups = {g["name"] for g in ctx["groups"]}
	# alice targets North/North-A -> only North-A's groups; never G-South.
	assert offered_groups == {"G-North-A", "G-North-A-Child"}
	assert "G-South" not in offered_groups


def test_non_admin_cannot_compose(gw):
	with pytest.raises(FlockPortalError):
		build_compose_context(user="dave", gateway=gw)


def test_is_compose_admin():
	assert is_compose_admin(("Flock Branch Admin",))
	assert is_compose_admin(("Flock Org Admin",))
	assert not is_compose_admin(("Flock Member",))


def test_assert_target_rejects_forged_cross_subtree_branch(gw):
	ctx = build_compose_context(user="alice", gateway=gw)
	# A tampered client posts branch=South (alice's sibling) — rejected.
	with pytest.raises(FlockPortalError):
		assert_target_in_context(branch="South", group=None, context=ctx)
	# A within-scope target is accepted.
	assert_target_in_context(branch="North-A", group=None, context=ctx)


def test_assert_target_rejects_missing_and_forged_group(gw):
	ctx = build_compose_context(user="alice", gateway=gw)
	with pytest.raises(FlockPortalError):
		assert_target_in_context(branch=None, group=None, context=ctx)
	# G-South is in a sibling branch -> not in alice's offered groups.
	with pytest.raises(FlockPortalError):
		assert_target_in_context(branch="North-A", group="G-South", context=ctx)
	# A scoped group is accepted.
	assert_target_in_context(branch="North-A", group="G-North-A", context=ctx)


def test_context_carries_catalogs_and_endpoints(gw):
	ctx = build_compose_context(user="alice", gateway=gw)
	assert ctx["categories"] == list(CATEGORY_OPTIONS)
	assert ctx["priorities"] == list(PRIORITY_OPTIONS)
	assert ctx["audience_roles"] == list(AUDIENCE_ROLE_OPTIONS)
	assert ctx["channels"] == list(CHANNEL_OPTIONS)
	assert ctx["endpoints"]["preview_audience"] == "flock_os.scheduling.preview_audience"
	assert ctx["endpoints"]["publish_announcement"] == "flock_os.scheduling.publish_announcement"
	assert ctx["organization"] == ORG


# --------------------------------------------------------------------------- #
# Reuse the FLO-57 fan-out gateway for the e2e audience resolution (same world).
# RecordingComposeGateway already exposes the SchedulingGateway-shaped reads.
# --------------------------------------------------------------------------- #


class FanoutWorld(RecordingComposeGateway):
	"""The same two-tree world, plus leader membership for fan-out resolution."""

	branch_leaders = {"HQ": [], "North": [], "North-A": ["M1"], "South": ["M2"]}
	group_leaders = {"G-North-A": ["M1"], "G-North-A-Child": ["M3"], "G-South": ["M2"]}

	def branch_exists(self, branch, organization):
		return BRANCH_ORG.get(branch) == organization

	def group_exists(self, group):
		return group in GROUP_TO_BRANCH

	def group_branch(self, group):
		return GROUP_TO_BRANCH.get(group)

	def branch_parent_of(self):
		return dict(BRANCH_PARENT)

	def branch_children_of(self):
		return _branch_children()

	def group_children_of(self):
		return {"G-North-A": ["G-North-A-Child"], "G-North-A-Child": [], "G-South": []}

	def leaders_in_branches(self, branches):
		out: list[str] = []
		for b in branches:
			out.extend(self.branch_leaders.get(b, []))
		return tuple(dict.fromkeys(out))

	def leaders_in_groups(self, groups):
		out: list[str] = []
		for g in groups:
			out.extend(self.group_leaders.get(g, []))
		return tuple(dict.fromkeys(out))


@pytest.fixture()
def world() -> FanoutWorld:
	return FanoutWorld()


def test_e2e_compose_preview_send_no_cross_subtree_leakage(world):
	"""FLO-60 DoD happy path: pick scope -> preview -> send -> subtree only.

	Exercises the real compose->preview->send chain through the pure primitives
	the FLO-94 transport orchestrates (no bench needed).
	"""
	# 1. Compose context for the Branch Admin (alice) — scoped, no leakage.
	ctx = build_compose_context(user="alice", gateway=world)
	assert "South" not in {b["name"] for b in ctx["branches"]}

	# 2. Pick a scope from the offered context (alice picks North).
	branch = "North"
	assert_target_in_context(branch=branch, group=None, context=ctx)

	# 3. Preview: audience = North subtree (North + North-A); no South, no HQ.
	audience_branches = resolve_audience_branches(branch, gateway=world)
	assert set(audience_branches) == {"North", "North-A"}
	assert "South" not in audience_branches

	# 4. Send: fan out to the scoped shard rooms; sibling leaders never receive it.
	sink = RecordingEventSink()
	publisher = RecordingRealtimePublisher()
	bus = EventBus(sink=sink)
	service = ScopedNotificationService(world, publisher, bus)
	result = service.fanout(
		scope=NotificationScope(organization=ORG, branch=branch),
		subject="Sunday Service",
		body="See you at 10.",
	)

	# Every publish went through the FLO-14 publisher on the RT notification event.
	assert {c["event"] for c in publisher.calls} == {RT_NOTIFICATION}
	# Exactly the subtree rooms — South's room never published (no leakage).
	assert set(publisher.rooms()) == {
		branch_notification_room("North"),
		branch_notification_room("North-A"),
	}
	assert branch_notification_room("South") not in publisher.rooms()
	# Leaders in the subtree received it; South's leader did not.
	assert set(result.audience.leaders) == {"M1"}
	assert "M2" not in result.audience.leaders
	# Exactly one canonical notification.sent event flowed (audit/outbox).
	assert result.event.name == NOTIFICATION_SENT
	assert [e for e, _r, _rm in sink.published if e.name == NOTIFICATION_SENT]
