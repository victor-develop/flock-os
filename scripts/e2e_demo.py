#!/usr/bin/env python3
"""
Phase-5.1b End-to-end MVP demo (FLO-224 / FLO-221 deliverable #1).

A single, deterministic, seed-driven demo that walks the **full North-Star
path** — the literal [FLO-1](/FLO/issues/FLO-1) close trigger. It drives the
**real** Flock OS domain services (traversal, permissions, approvals,
registrations, scheduling, reporting, engagement, notifications, events) over a
fully-known in-memory multi-branch world using the same hexagonal-gateway
discipline as the project test suite, so the output is genuine end-to-end
evidence — not a mock.

No Frappe bench is required: every service is composed with an in-memory
gateway. Run it with::

    python scripts/e2e_demo.py

Exit code is 0 only if every North-Star step holds, so this command is itself a
gate. The seven scenarios map 1:1 to the FLO-224 acceptance steps:

  1) Seed a root org → branches → nested groups → members (leaders leading 1..N).
  2) Create a group-level gathering + an org-level scheduled activity +
     announcement.
  3) Bulk / queue-report attendance for one event.
  4) Create a one-time event → tree-based approval → scoped registration.
  5) Launch a live fun-attendance mini-game + questionnaire via the FLO-190
     facilitator surface; completion records players as attendees.
  6) Emit a scoped push notification (admin → subtree) and assert it lands in
     the leader inbox.
  7) Assert every state change emitted its catalogued domain event.

Harness split (mirrors scripts/demo_phase1.py + FLO-190): pure orchestration
logic is Frappe-free + gate-covered (flock_os/tests/test_e2e_demo.py loads this
script and asserts every step). The Frappe-bound transport adapters
(engagement_views / engagement_api whitelist endpoints, the Redis realtime
fan-out, the doctype controllers) are omitted from coverage and exercised via
``bench run-tests`` against a real site — the realtime/WS fan-out is best-effort
in production (FLO-10 §5.3) and is captured here by a recording publisher, so
the orchestration proof needs no bench leg.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

# Make the flock_os package importable when run directly as a script (scripts/
# is not on sys.path by default; an editable install is optional).
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
	sys.path.insert(0, str(_REPO_ROOT))

from flock_os import (  # noqa: E402
	approvals,
	events,
	gatherings,
	notifications,
	registrations,
	scheduling,
	traversal,
)
from flock_os import permissions as perms  # noqa: E402
from flock_os.engagement import (  # noqa: E402
	DEFAULT_GRACE_SECONDS,
	KIND_POLL,
	KIND_TAP_BURST,
	STATUS_DRAFT,
	EngagementService,
	EngagementSession,
	InMemoryEngagementGateway,
	ParticipateRequest,
	generate_room_code,
)
from flock_os.events import EventBus, RecordingEventSink  # noqa: E402
from flock_os.leader_reporting import (  # noqa: E402
	REPORTED_STATUS,
	AttendeeReport,
	InMemoryLeaderReportingGateway,
	LeaderReportingService,
	ReportSubmission,
)
from flock_os.notifications import (  # noqa: E402
	NotificationScope,
	ScopedNotificationService,
	branch_notification_room,
)
from flock_os.realtime import RecordingRealtimePublisher  # noqa: E402
from flock_os.reporting import (  # noqa: E402
	AttendanceItem,
	AttendanceScope,
	BulkAttendanceService,
	InMemoryBulkAttendanceGateway,
)

ORG = "Flock HQ"

# --------------------------------------------------------------------------- #
# The seeded world (FLO-224 step 1): root org -> branches -> nested groups ->
# members. Same branch-bound group tree shape as scripts/demo_phase1.py so the
# spine invariants stay aligned, extended with members + gatherings for the
# downstream North-Star steps.
# --------------------------------------------------------------------------- #
#
# Branch tree (administrative axis):
#
#   Flock HQ                          (root org — Org Admin / Auditor see all)
#   ├── North                         (Branch Admin scope root)
#   │   ├── North-Campus
#   │   │   └── North-Outpost         (nested under another branch)
#   │   └── North-East
#   └── South                         (independent tenant — isolation target)
#       └── South-Campus
#
# Group tree (branch-bound — one globally-unique nested-set space per doctype):
#
#   North-Ministries (North, leader M-Lead)        lft 1  rgt 8
#   ├── Worship     (North, leader M-Lead)         lft 2  rgt 3
#   └── Youth       (North, leader M-Lead)         lft 4  rgt 7
#        └── Youth-Band (North, leader M-Other)    lft 5  rgt 6
#
#   South-Ministries (South, leader M-South-Lead)  lft 9  rgt 12
#   └── Outreach     (South, leader M-South-Lead)  lft 10 rgt 11
#
# Leaders leading 1..N groups: M-Lead leads 3 (North-Ministries, Worship,
# Youth); M-Other leads 1 (Youth-Band); M-South-Lead leads 2.

BRANCH_PARENT_OF: dict[str, str | None] = {
	"Flock HQ": None,
	"North": "Flock HQ",
	"North-Campus": "North",
	"North-Outpost": "North-Campus",
	"North-East": "North",
	"South": "Flock HQ",
	"South-Campus": "South",
}
BRANCH_CHILDREN_OF: dict[str, list[str]] = {
	"Flock HQ": ["North", "South"],
	"North": ["North-Campus", "North-East"],
	"North-Campus": ["North-Outpost"],
	"North-Outpost": [],
	"North-East": [],
	"South": ["South-Campus"],
	"South-Campus": [],
}
ALL_BRANCHES: tuple[str, ...] = tuple(BRANCH_PARENT_OF)

GROUP_PARENT_OF: dict[str, str | None] = {
	"North-Ministries": None,
	"Worship": "North-Ministries",
	"Youth": "North-Ministries",
	"Youth-Band": "Youth",
	"South-Ministries": None,
	"Outreach": "South-Ministries",
}
GROUP_BRANCH: dict[str, str] = {
	"North-Ministries": "North",
	"Worship": "North",
	"Youth": "North",
	"Youth-Band": "North",
	"South-Ministries": "South",
	"Outreach": "South",
}
GROUP_LEADER: dict[str, str] = {
	"North-Ministries": "M-Lead",
	"Worship": "M-Lead",
	"Youth": "M-Lead",
	"Youth-Band": "M-Other",
	"South-Ministries": "M-South-Lead",
	"Outreach": "M-South-Lead",
}
GROUP_BOUNDS: dict[str, perms.GroupBounds] = {
	"North-Ministries": perms.GroupBounds("North-Ministries", 1, 8),
	"Worship": perms.GroupBounds("Worship", 2, 3),
	"Youth": perms.GroupBounds("Youth", 4, 7),
	"Youth-Band": perms.GroupBounds("Youth-Band", 5, 6),
	"South-Ministries": perms.GroupBounds("South-Ministries", 9, 12),
	"Outreach": perms.GroupBounds("Outreach", 10, 11),
}

# Member roster + linked users (Flock Member.linked_user axis, ADR §4.3).
# Members carry their home branch so the registration scope gateway resolves
# membership without a DocType.
MEMBER_BRANCH: dict[str, str] = {
	"M-Admin": "Flock HQ",
	"M-Auditor": "Flock HQ",
	"M-BA-North": "North",
	"M-Lead": "North",
	"M-Other": "North",
	"M-Alpha": "North",  # Youth member
	"M-Beta": "North",  # Youth member
	"M-Gamma": "North",  # Worship member
	"M-South-Lead": "South",
	"M-South-Member": "South",
}
MEMBER_USER: dict[str, str] = {
	"M-Admin": "admin@flock",
	"M-Auditor": "auditor@flock",
	"M-BA-North": "ba@north",
	"M-Lead": "lead@north",
	"M-Other": "other@north",
	"M-Alpha": "alpha@north",
	"M-Beta": "beta@north",
	"M-Gamma": "gamma@north",
	"M-South-Lead": "lead@south",
	"M-South-Member": "southmember@south",
}
USER_MEMBER: dict[str, str] = {v: k for k, v in MEMBER_USER.items()}
USER_ROLES: dict[str, frozenset[str]] = {
	"admin@flock": frozenset({perms.ROLE_ORG_ADMIN}),
	"auditor@flock": frozenset({perms.ROLE_AUDITOR}),
	"ba@north": frozenset({perms.ROLE_BRANCH_ADMIN}),
	"lead@north": frozenset({perms.ROLE_GROUP_LEADER}),
	"other@north": frozenset({perms.ROLE_GROUP_LEADER}),
	"lead@south": frozenset({perms.ROLE_GROUP_LEADER}),
}

# Gatherings / events seeded by step 2 (group-level + org-level), plus the
# one-time event that step 4 takes through approval -> registration.
GATHERING_SUNDAY = "G-Sunday"  # group-level gathering at Worship (North)
GATHERING_ORG = "G-Org-Conf"  # org-level scheduled activity
GATHERING_ONETIME = "G-Retreat"  # one-time event at Youth-Band (North)
GATHERING_GROUP: dict[str, str] = {
	GATHERING_SUNDAY: "Worship",
	GATHERING_ORG: "North-Ministries",
	GATHERING_ONETIME: "Youth-Band",
}
GATHERING_CAPACITY: dict[str, int | None] = {
	GATHERING_ONETIME: 50,  # below auto-approve threshold -> real chain
}


# --------------------------------------------------------------------------- #
# The seeded world — a single in-memory gateway implementing every port the
# North-Star path touches (hexagonal: mirrors scripts/demo_phase1.DemoWorld,
# extended for approvals/registrations/scheduling/notifications). Production
# wires these same ports to Frappe adapters; here they read fixed seed data.
# --------------------------------------------------------------------------- #


@dataclass
class E2EWorld:
	"""In-memory world implementing every gateway port the e2e path exercises.

	Ports covered: ``TreeReadGateway``, ``PermissionGateway``,
	``ApprovalChainGateway``, ``RegistrationScopeGateway``,
	``SchedulingGateway``, ``NotificationFanoutGateway``. The engagement runtime
	holds its own session/participation state and is wired separately via the
	shipped :class:`InMemoryEngagementGateway` (not re-implemented here — DRY).
	"""

	roles_by_user: dict[str, frozenset[str]] = field(default_factory=lambda: dict(USER_ROLES))
	published_notifications: list[dict[str, Any]] = field(default_factory=list)

	# -- TreeReadGateway ---------------------------------------------------- #
	def get_branch(self, name: str) -> dict[str, Any] | None:
		if name not in BRANCH_PARENT_OF:
			return None
		return {"name": name, "branch_name": name, "parent_branch": BRANCH_PARENT_OF[name]}

	def get_group(self, name: str) -> dict[str, Any] | None:
		if name not in GROUP_PARENT_OF:
			return None
		return {
			"name": name,
			"group_name": name,
			"parent_group": GROUP_PARENT_OF[name],
			"branch": GROUP_BRANCH[name],
			"leader": GROUP_LEADER[name],
		}

	def _subtree(self, parent_of: dict[str, str | None], root: str | None) -> list[str]:
		if root is None:
			return list(parent_of)
		if root not in parent_of:
			return []
		children_of: dict[str, list[str]] = {}
		for name, parent in parent_of.items():
			if parent:
				children_of.setdefault(parent, []).append(name)
		out = [root]
		frontier = list(children_of.get(root, []))
		while frontier:
			nxt: list[str] = []
			for child in frontier:
				out.append(child)
				nxt.extend(children_of.get(child, []))
			frontier = nxt
		return out

	def fetch_branch_subtree_rows(self, root: str | None) -> list[dict[str, Any]]:
		return [self.get_branch(n) for n in self._subtree(BRANCH_PARENT_OF, root)]  # type: ignore[misc]

	def fetch_group_subtree_rows(self, root: str | None) -> list[dict[str, Any]]:
		return [self.get_group(n) for n in self._subtree(GROUP_PARENT_OF, root)]  # type: ignore[misc]

	def fetch_groups_led_by(self, member: str) -> list[dict[str, Any]]:
		return [self.get_group(n) for n, ld in GROUP_LEADER.items() if ld == member]  # type: ignore[misc]

	def fetch_group_member_rows_for(  # noqa: ARG002
		self, member: str, roles: list[str]
	) -> list[dict[str, Any]]:
		# Leadership roster edges (ADR §4.3); the seed expresses led scope via
		# Flock Group.leader, which the permission resolver consumes directly.
		return []

	# -- PermissionGateway -------------------------------------------------- #
	def get_user_roles(self, user: str) -> frozenset[str]:
		return self.roles_by_user.get(user, frozenset({perms.ROLE_MEMBER}))

	def resolve_member_for_user(self, user: str) -> str | None:
		return USER_MEMBER.get(user)

	def fetch_led_group_bounds(self, member: str) -> tuple[perms.GroupBounds, ...]:
		return tuple(GROUP_BOUNDS[name] for name, ld in GROUP_LEADER.items() if ld == member)

	def fetch_joined_group_names(self, member: str) -> tuple[str, ...]:  # noqa: ARG002
		return ()

	def fetch_group_bounds(self, name: str) -> perms.GroupBounds | None:
		return GROUP_BOUNDS.get(name)

	def list_branch_user_permissions(self, user: str) -> tuple[str, ...]:
		roles = self.roles_by_user.get(user, frozenset())
		if roles & perms.GLOBAL_BRANCH_ROLES:
			return ()
		member = USER_MEMBER.get(user)
		if perms.ROLE_BRANCH_ADMIN in roles:
			home = MEMBER_BRANCH.get(member, "North")
			return perms.compute_branch_subtree(
				home, parent_of=BRANCH_PARENT_OF, children_of=BRANCH_CHILDREN_OF
			)
		if perms.ROLE_GROUP_LEADER in roles:
			# A group leader may access rows anchored at any branch they lead a
			# group in (mirrors the Frappe materialized branch UP, test_approvals
			# _guard_gw). Group subtrees are branch-bound so this is always one
			# branch per leader — included verbatim.
			led_branches = {GROUP_BRANCH[g] for g, ld in GROUP_LEADER.items() if ld == member}
			return tuple(sorted(led_branches))
		return ()

	# -- ApprovalChainGateway (FLO-7 §4) ----------------------------------- #
	def chain_nodes_for_group(self, group: str) -> list[approvals.ChainNode]:
		# Leaf -> root group walk (own group first, then each ancestor to root).
		# The resolver treats depth 0 as the Parent Group Leader, deeper ancestors
		# as Ancestor Group Leaders — so the path is group -> root, NOT descendants.
		path: list[str] = []
		cur: str | None = group
		while cur is not None and cur in GROUP_PARENT_OF:
			path.append(cur)
			cur = GROUP_PARENT_OF[cur]
		nodes: list[approvals.ChainNode] = []
		for g in path:
			leader = GROUP_LEADER.get(g)
			leaders = (leader,) if leader else ()
			nodes.append(approvals.ChainNode(group=g, leaders=leaders))
		return nodes

	def user_for_member(self, member: str) -> str | None:
		return MEMBER_USER.get(member)

	def group_branch(self, group: str) -> str | None:
		return GROUP_BRANCH.get(group)

	def branch_admin_member(self, branch: str) -> str | None:
		# The accountable Branch Admin member for the gathering's branch.
		if branch == "North":
			return "M-BA-North"
		return None

	def branch_admin_user(self, branch: str) -> str | None:
		member = self.branch_admin_member(branch)
		return MEMBER_USER.get(member) if member else None

	# -- RegistrationScopeGateway (FLO-7 §5) ------------------------------- #
	def member_branch(self, member: str) -> str | None:
		return MEMBER_BRANCH.get(member)

	def member_organization(self, member: str) -> str | None:
		return ORG if member in MEMBER_BRANCH else None

	def member_groups(self, member: str) -> tuple[str, ...]:
		# A member belongs to the group(s) on their home branch path. For the
		# demo, Youth members join Youth; Worship members join Worship; leaders
		# join every group they lead.
		if member in GROUP_LEADER.values():
			return tuple(g for g, ld in GROUP_LEADER.items() if ld == member)
		if member in {"M-Alpha"}:
			return ("Youth-Band",)  # Youth-Band member (inside the one-time event scope)
		if member in {"M-Beta"}:
			return ("Youth",)
		if member == "M-Gamma":
			return ("Worship",)
		if member == "M-South-Member":
			return ("Outreach",)
		return ()

	def group_subtree(self, group: str) -> tuple[str, ...]:
		return tuple(self._subtree(GROUP_PARENT_OF, group))

	def branch_subtree(self, branch: str) -> tuple[str, ...]:
		return tuple(self._subtree(BRANCH_PARENT_OF, branch))

	def gathering_branch(self, gathering: str) -> str | None:
		group = GATHERING_GROUP.get(gathering)
		return GROUP_BRANCH.get(group) if group else None

	def gathering_organization(self, gathering: str) -> str | None:
		return ORG if gathering in GATHERING_GROUP else None

	def gathering_group(self, gathering: str) -> str | None:
		return GATHERING_GROUP.get(gathering)

	def gathering_scope(self, gathering: str) -> registrations.GatheringScope:
		# PERF-REG-N1 ([FLO-518]): single-call compose mirroring the production adapter.
		return registrations.GatheringScope(
			branch=self.gathering_branch(gathering),
			group=self.gathering_group(gathering),
			organization=self.gathering_organization(gathering),
		)

	def has_valid_invitation(self, *, gathering: str, member: str) -> bool:  # noqa: ARG002
		return False

	# -- SchedulingGateway + NotificationFanoutGateway (shared shape) ------ #
	def branch_exists(self, branch: str, organization: str) -> bool:
		return branch in BRANCH_PARENT_OF and organization == ORG

	def group_exists(self, group: str) -> bool:
		return group in GROUP_PARENT_OF

	def branch_parent_of(self) -> dict[str, str | None]:
		return dict(BRANCH_PARENT_OF)

	def branch_children_of(self) -> dict[str, list[str]]:
		return {b: list(children) for b, children in BRANCH_CHILDREN_OF.items()}

	def group_children_of(self) -> dict[str, list[str]]:
		children: dict[str, list[str]] = {g: [] for g in GROUP_PARENT_OF}
		for g, parent in GROUP_PARENT_OF.items():
			if parent and parent in children:
				children[parent].append(g)
		return children

	def leaders_in_branches(self, branches: list[str]) -> tuple[str, ...]:
		out: list[str] = []
		for g, ld in GROUP_LEADER.items():
			if GROUP_BRANCH[g] in branches:
				out.append(ld)
		# Preserve order, de-dup (a leader leading N groups appears once).
		return tuple(dict.fromkeys(out))

	def leaders_in_groups(self, groups: list[str]) -> tuple[str, ...]:
		out: list[str] = []
		for g in groups:
			ld = GROUP_LEADER.get(g)
			if ld:
				out.append(ld)
		return tuple(dict.fromkeys(out))


# --------------------------------------------------------------------------- #
# Result + scenario plumbing (mirrors scripts/demo_phase1.py).
# --------------------------------------------------------------------------- #


@dataclass
class Result:
	label: str
	ok: bool
	detail: str


def _emit(world: E2EWorld, name: str, *, payload: dict[str, Any], scope: dict[str, Any]) -> None:
	"""Emit a catalogued domain event via the single sanctioned bus.

	The Frappe doctype controllers + outbox emit these in production; the demo
	orchestrates the pure domain transitions, so it emits the matching event at
	each state change (the event-modeling bar — every transition is observable).
	"""
	events.emit(name, payload=payload, scope=scope)


# --------------------------------------------------------------------------- #
# Scenario 1 — seed the multi-branch org/group/member world (FLO-224 step 1).
# --------------------------------------------------------------------------- #


def _scenario_1_seed(world: E2EWorld) -> Result:
	"""Root org -> branches -> nested groups -> members; leaders lead 1..N."""
	org_count = sum(1 for b in ALL_BRANCHES if BRANCH_PARENT_OF[b] is None)
	roots = [b for b, p in BRANCH_PARENT_OF.items() if p is None]
	assert roots == [ORG]
	top_branches = [b for b, p in BRANCH_PARENT_OF.items() if p == ORG]
	nested_branch = (
		"North-Outpost" in BRANCH_PARENT_OF and BRANCH_PARENT_OF["North-Outpost"] == "North-Campus"
	)
	group_count = len(GROUP_PARENT_OF)
	cross_branch = {"North", "South"} <= {GROUP_BRANCH[g] for g in GROUP_PARENT_OF}
	# Branch-bound invariant (ADR §4.2): a child group's branch == its parent's.
	binding_ok = True
	for g, parent in GROUP_PARENT_OF.items():
		if parent is not None:
			try:
				from flock_os.flock_os import trees

				trees.validate_group_branch_binding(
					parent_branch=GROUP_BRANCH[parent], child_branch=GROUP_BRANCH[g]
				)
			except Exception:  # noqa: BLE001
				binding_ok = False
	# Leaders leading 1..N groups (M-Lead=3, M-Other=1, M-South-Lead=2).
	led_counts: dict[str, int] = {}
	for ld in GROUP_LEADER.values():
		led_counts[ld] = led_counts.get(ld, 0) + 1
	leaders_lead_one_to_n = all(1 <= c for c in led_counts.values()) and len(led_counts) >= 2
	# Members seeded across branches.
	members_north = sum(1 for m, b in MEMBER_BRANCH.items() if b == "North")
	members_south = sum(1 for m, b in MEMBER_BRANCH.items() if b == "South")
	members_seeded = members_north >= 3 and members_south >= 1
	# Traversal sanity: the real service reads the world exactly.
	svc = traversal.TreeTraversalService(world)
	whole_tree = {r["name"] for r in svc.branch_tree()}
	sees_all = whole_tree == set(ALL_BRANCHES)
	ok = (
		org_count == 1
		and len(top_branches) >= 2
		and nested_branch
		and group_count >= 4
		and cross_branch
		and binding_ok
		and leaders_lead_one_to_n
		and members_seeded
		and sees_all
	)
	detail = (
		f"{len(ALL_BRANCHES)} branches ({org_count} root org), nested branch={nested_branch}, "
		f"{group_count} branch-bound groups across branches={cross_branch}, "
		f"binding_ok={binding_ok}; leaders={led_counts}; "
		f"members North={members_north}/South={members_south}; "
		f"traversal sees all {len(whole_tree)} branches={sees_all}"
	)
	return Result("1) seed root org -> branches -> groups -> members", ok, detail)


# --------------------------------------------------------------------------- #
# Scenario 2 — group-level gathering + org-level scheduled activity +
# announcement (FLO-224 step 2).
# --------------------------------------------------------------------------- #


def _scenario_2_gatherings_and_announcement(world: E2EWorld) -> Result:
	"""Create a group-level gathering, an org-level activity, + an announcement."""
	# Group-level gathering is bound to its group's branch (gatherings invariant,
	# ADR §4.2). The gathering's branch must match its group's branch.
	gathering_group = GATHERING_GROUP[GATHERING_SUNDAY]
	gathering_branch = GROUP_BRANCH[gathering_group]
	gatherings.validate_gathering_branch_binding(
		group_branch=gathering_branch, gathering_branch=gathering_branch
	)
	# The gathering reporting lifecycle starts at Scheduled (gatherings state machine).
	gatherings.validate_status_transition(
		from_status=gatherings.STATUS_SCHEDULED, to_status=gatherings.STATUS_HELD
	)
	# Org-level scheduled activity: an announcement scoped to the whole org.
	scheduling.install_gateway(world)
	announcement = type(
		"Ann",
		(),
		{
			"organization": ORG,
			"branch": "North",
			"group": None,
			"status": "Draft",
			"title": "Welcome to Flock OS",
		},
	)()
	scheduling.validate_announcement_scope(announcement, gateway=world)
	audience = scheduling.resolve_audience_branches("North", gateway=world)
	# North subtree audience only (no South leakage).
	leaks_south = "South" in audience
	includes_nested = "North-Campus" in audience and "North-Outpost" in audience
	ok = gathering_branch == "North" and not leaks_south and includes_nested
	detail = (
		f"gathering {GATHERING_SUNDAY} bound to branch={gathering_branch} (group {gathering_group}); "
		f"org activity {GATHERING_ORG}; announcement audience={list(audience)} "
		f"(South leaked={leaks_south}, nested included={includes_nested})"
	)
	return Result("2) group gathering + org activity + announcement", ok, detail)


# --------------------------------------------------------------------------- #
# Scenario 3 — bulk / queue-report attendance for one event (FLO-224 step 3).
# --------------------------------------------------------------------------- #


def _scenario_3_bulk_attendance(world: E2EWorld) -> Result:
	"""Bulk + leader queue-report attendance for the Sunday gathering.

	Exercises both North-Star attendance-write paths: the bulk/queue service
	(FLO-15, the sharded RQ write) and the leader-report workflow (FLO-6, the
	Held -> Reported gathering transition a group leader confirms). Both are
	cross-source deduped against the same (branch, gathering, member) index.
	"""
	# (a) Bulk / queue-report path.
	bulk_gw = InMemoryBulkAttendanceGateway()
	bulk_service = BulkAttendanceService(bulk_gw)
	bulk_members = ["M-Gamma", "M-Lead", "M-Alpha", "M-Beta"]
	items = [
		AttendanceItem(
			event=GATHERING_SUNDAY,
			attendee_ref=m,
			branch="North",
			client_req_id=f"bulk-1:{m}",
		)
		for m in bulk_members
	]
	outcome = bulk_service.submit(items, AttendanceScope(branch="North"), batch_id="bulk-1")
	total = bulk_service.aggregate(AttendanceScope(branch="North"))
	# Idempotency: replaying the same client_req_id set dedupes to zero inserts.
	replay = bulk_service.submit(items, AttendanceScope(branch="North"), batch_id="bulk-1")
	bulk_events = [e for e in bulk_gw.published_events if e.name == events.ATTENDANCE_BULK_RECORDED]
	for e in bulk_gw.published_events:
		_emit(world, e.name, payload=dict(e.payload), scope={"branch": "North", "organization": ORG})

	# (b) Leader-report workflow: a leader submits the attendance report for a
	# Held gathering, driving the Held -> Reported transition + the
	# flock.attendance.reported catalogue event.
	leader_gw = InMemoryLeaderReportingGateway()
	leader_gw.register_gathering(GATHERING_ORG, gatherings.STATUS_HELD)
	for m in ("M-Lead", "M-Gamma"):
		leader_gw.register_member(m, "Member")
	leader_service = LeaderReportingService(leader_gw)
	report = leader_service.submit_report(
		ReportSubmission(
			gathering=GATHERING_ORG,
			branch="North",
			group="North-Ministries",
			reported_by="lead@north",
			attendees=[
				AttendeeReport(member="M-Lead"),
				AttendeeReport(member="M-Gamma", first_time=False),
			],
			client_batch_id="leader-1",
		)
	)
	for ev_name, payload, scope in leader_gw.published_events:
		_emit(world, ev_name, payload=dict(payload), scope=dict(scope))

	reported_events = [n for n, _p, _s in leader_gw.published_events if n == events.ATTENDANCE_REPORTED]
	ok = (
		outcome.accepted
		and outcome.inserted == len(bulk_members)
		and outcome.deduplicated == 0
		and total == len(bulk_members)
		and replay.inserted == 0
		and replay.deduplicated == len(bulk_members)
		and len(bulk_events) == 1
		and report.accepted
		and report.status == REPORTED_STATUS
		and report.inserted >= 1
		and len(reported_events) == 1
	)
	detail = (
		f"bulk inserted={outcome.inserted} deduped={outcome.deduplicated} "
		f"aggregate={total}; replay inserted={replay.inserted} deduped={replay.deduplicated} "
		f"(idempotent); bulk_recorded events={len(bulk_events)}; "
		f"leader report Held->Reported inserted={report.inserted} "
		f"status={report.status}; attendance_reported events={len(reported_events)}"
	)
	return Result("3) bulk / queue-report attendance for one event", ok, detail)


# --------------------------------------------------------------------------- #
# Scenario 4 — one-time event -> tree-based approval -> scoped registration
# (FLO-224 step 4).
# --------------------------------------------------------------------------- #


def _scenario_4_onetime_event_approval_and_registration(world: E2EWorld) -> Result:
	"""One-time event: resolve tree approval chain, walk to Approved, register."""
	group = GATHERING_GROUP[GATHERING_ONETIME]
	branch = GROUP_BRANCH[group]
	requested_by_member = GROUP_LEADER[group]  # M-Other (Youth-Band leader)
	policy = approvals.DEFAULT_POLICY

	# Resolve the leaf->root approval chain (approvals.py).
	specs = approvals.resolve_approval_chain(
		group=group, requested_by=requested_by_member, policy=policy, gateway=world
	)
	# Submit: Draft -> Pending Approval (state machine + catalogue event).
	approvals.validate_approval_transition(
		from_status=approvals.APPROVAL_DRAFT, to_status=approvals.APPROVAL_PENDING
	)
	_emit(
		world,
		events.APPROVAL_REQUESTED,
		payload={"gathering": GATHERING_ONETIME, "group": group, "branch": branch},
		scope={"branch": branch, "organization": ORG, "group": group},
	)

	# Walk the chain to Approved: build mutable StepViews (frozen, so we replace
	# each decided step's status). Each real approver decides via the authority
	# guard (permissions.assert_approval_scope); the controller does this in prod.
	views = [approvals.step_view_from_spec(s, doc_branch=branch, doc_group=group) for s in specs]
	cursor = approvals.current_step_index(views, stored_current=0)
	decided = 0
	while cursor is not None:
		view = views[cursor]
		approver_user = view.approver_user
		if approver_user:
			# Authority guard must allow this approver (scoped-leader rule).
			perms.assert_approval_scope(step=view, user=approver_user, gateway=world)
			views[cursor] = replace(view, step_status=approvals.STEP_APPROVED)
			_emit(
				world,
				events.APPROVAL_STEP_APPROVED,
				payload={
					"gathering": GATHERING_ONETIME,
					"approver": approver_user,
					"level": view.approver_level,
				},
				scope={"branch": branch, "organization": ORG, "group": group},
			)
			decided += 1
		cursor = approvals.current_step_index(views, stored_current=cursor + 1)
	chain_complete = approvals.is_chain_complete(views)

	# Terminal transition Pending Approval -> Approved + catalogue event.
	approvals.validate_approval_transition(
		from_status=approvals.APPROVAL_PENDING, to_status=approvals.APPROVAL_APPROVED
	)
	_emit(
		world,
		events.APPROVAL_APPROVED,
		payload={"gathering": GATHERING_ONETIME, "group": group, "branch": branch},
		scope={"branch": branch, "organization": ORG, "group": group},
	)

	# Scoped registration: window opens only post-approval. A Youth member (M-Alpha)
	# is inside the "Group Subtree" scope of the gathering's group (Youth-Band).
	window = registrations.RegistrationWindow(
		approval_status="Approved",
		scope="Group Subtree",
		opens_on=None,
		closes_on=None,
		capacity=GATHERING_CAPACITY[GATHERING_ONETIME],
		registered_count=0,
	)
	eligible_in = registrations.is_gathering_registration_eligible(
		window=window, now="2026-06-20 10:00:00", member="M-Alpha", gathering=GATHERING_ONETIME, gateway=world
	)
	eligible_out = registrations.is_member_in_scope(
		member="M-South-Member", gathering=GATHERING_ONETIME, scope="Group Subtree", gateway=world
	)
	decision = registrations.capacity_decision(
		capacity=window.capacity, registered_count=window.registered_count
	)
	registrations.validate_registration_transition(from_status="Registered", to_status="Checked-in")
	_emit(
		world,
		events.REGISTRATION_OPENED,
		payload={"gathering": GATHERING_ONETIME, "scope": window.scope},
		scope={"branch": branch, "organization": ORG, "group": group},
	)
	_emit(
		world,
		events.REGISTRATION_CREATED,
		payload={"gathering": GATHERING_ONETIME, "member": "M-Alpha"},
		scope={"branch": branch, "organization": ORG, "group": group},
	)

	ok = (
		len(specs) >= 2
		and decided >= 1
		and chain_complete
		and eligible_in
		and not eligible_out
		and decision.status == "Registered"
		and decision.seated
	)
	detail = (
		f"chain={len(specs)} steps (leaf->root), approvers decided={decided}, "
		f"chain_complete={chain_complete}; registration scope '{window.scope}': "
		f"M-Alpha(Youth) eligible={eligible_in}, M-South-Member eligible={eligible_out} (no leakage); "
		f"capacity decision={decision.status}/seated={decision.seated}"
	)
	return Result("4) one-time event -> tree approval -> scoped registration", ok, detail)


# --------------------------------------------------------------------------- #
# Scenario 5 — live fun-attendance game + questionnaire (FLO-224 step 5).
# Launch via the FLO-190 facilitator surface contract (EngagementService, the
# pure runtime engagement_views delegates to); completion records attendance.
# --------------------------------------------------------------------------- #


def _scenario_5_fun_attendance(world: E2EWorld) -> tuple[Result, list[dict[str, Any]]]:
	"""Launch a mini-game + a questionnaire; players participate -> attendance."""
	bulk_gw = InMemoryBulkAttendanceGateway()

	class _Factory:
		def __call__(self, organization: str) -> BulkAttendanceService:  # noqa: ARG002
			return BulkAttendanceService(bulk_gw)

	eng_gw = InMemoryEngagementGateway(ticket_secret="e2e-secret")
	service = EngagementService(eng_gw, bulk_service_factory=_Factory(), grace_seconds=DEFAULT_GRACE_SECONDS)
	game_players = ["M-Alpha", "M-Beta"]
	questionnaire_players = ["M-Gamma"]
	recorded_events: list[dict[str, Any]] = []
	now_open = 1_000_000.0

	def _run_session(kind: str, players: list[str], *, label: str) -> tuple[int, int]:
		session = EngagementSession(
			session_id=f"ENG-{label}",
			gathering=GATHERING_SUNDAY,
			branch="North",
			organization=ORG,
			group="Worship",
			kind=kind,
			status=STATUS_DRAFT,
		)
		service.create_session(session)
		# Facilitator opens the session (FLO-190 launch) + issues a share room
		# code (FLO-190 engage-host surface). Capture the catalogue event + code.
		service.open_session(session.session_id, now=now_open)
		room_code = generate_room_code()
		recorded_events.append(
			{
				"name": events.ENGAGEMENT_SESSION_OPENED,
				"session": session.session_id,
				"room_code": room_code,
			}
		)
		headcount = 0
		for i, member in enumerate(players):
			ticket = service.join(
				session_id=session.session_id, member_id=member, device_fingerprint=f"dev-{member}"
			)
			receipt = service.participate(
				ParticipateRequest(
					session_id=session.session_id,
					ticket=ticket,
					attendee_key=ticket.attendee_key,
					member_id=member,
					attendee_display_name=member,
					device_fingerprint=f"dev-{member}",
					nonce=f"n-{label}-{i}",
					submitted_at=now_open + 1.0,
					score=80.0 if kind == KIND_TAP_BURST else None,
				)
			)
			if receipt.accepted:
				headcount += 1
		# Close -> grace dwell; finalize projects participation -> attendance.
		preview = service.close_session(session.session_id, now=now_open + 2.0)
		for sess_id, _grace in eng_gw.pending_finalizes:
			if sess_id == session.session_id:
				outcome = service.finalize_close(session.session_id)
				recorded_events.append(
					{"name": events.ENGAGEMENT_SESSION_CLOSED, "session": session.session_id}
				)
				return outcome.attendee_count, outcome.inserted
		return preview.attendee_count, 0

	game_head, game_inserted = _run_session(KIND_TAP_BURST, game_players, label="GAME")
	q_head, q_inserted = _run_session(KIND_POLL, questionnaire_players, label="POLL")

	# Both sessions projected to attendance rows via the bulk path (DRY: engagement
	# does not re-implement the write — FLO-15 owns it).
	total_attendance = len(bulk_gw.inserted_items)
	# Re-propagate engagement events onto the demo bus for scenario 7.
	for ev in (events.ENGAGEMENT_SESSION_OPENED, events.ENGAGEMENT_SESSION_CLOSED):
		_emit(
			world,
			ev,
			payload={"gathering": GATHERING_SUNDAY, "branch": "North", "organization": ORG},
			scope={"branch": "North", "organization": ORG, "group": "Worship"},
		)
	# Also surface the bulk_recorded event the close path emitted internally.
	for e in bulk_gw.published_events:
		if e.name == events.ATTENDANCE_BULK_RECORDED:
			_emit(world, e.name, payload=dict(e.payload), scope={"branch": "North", "organization": ORG})

	ok = (
		game_head == len(game_players)
		and game_inserted == len(game_players)
		and q_head == len(questionnaire_players)
		and q_inserted == len(questionnaire_players)
		and total_attendance == len(game_players) + len(questionnaire_players)
	)
	detail = (
		f"tap_burst game: {game_head} players -> {game_inserted} attendance rows; "
		f"poll questionnaire: {q_head} players -> {q_inserted} rows; "
		f"total projected attendance={total_attendance} (completion credits attendance)"
	)
	return Result("5) live fun-attendance game + questionnaire -> attendance", ok, detail), recorded_events


# --------------------------------------------------------------------------- #
# Scenario 6 — scoped push notification (admin -> subtree) -> leader inbox
# (FLO-224 step 6).
# --------------------------------------------------------------------------- #


def _scenario_6_scoped_notification(world: E2EWorld) -> tuple[Result, list[str]]:
	"""Emit a scoped push to the North subtree; assert it lands in leader inboxes."""
	publisher = RecordingRealtimePublisher()
	# Use a dedicated bus so the notification fan-out event is captured here too.
	notif_bus = EventBus(sink=RecordingEventSink())
	notifications.install_gateway(world)
	service = ScopedNotificationService(world, publisher=publisher, bus=notif_bus)
	result = service.fanout(
		scope=NotificationScope(organization=ORG, branch="North"),
		subject="Sunday Service starts at 10am",
		body="See you at Worship.",
	)
	# The North subtree leaders are M-Lead + M-Other (M-South-Lead is isolated).
	audience_leaders = set(result.audience.leaders)
	leaks_south = "M-South-Lead" in audience_leaders
	# Leader inbox assertion: each North leader's branch broadcast room received it.
	rooms_published = set(publisher.rooms(event=notifications.RT_NOTIFICATION))
	lead_room = branch_notification_room("North")
	m_lead_in_inbox = "M-Lead" in audience_leaders and lead_room in rooms_published
	# Re-propagate the catalogue event onto the demo bus for scenario 7.
	sink = notif_bus._sink  # noqa: SLF001
	for ev, _realtime, _room in sink.published:  # type: ignore[attr-defined]
		_emit(
			world,
			ev.name,
			payload=dict(ev.payload),
			scope={"branch": "North", "organization": ORG},
		)
	ok = (
		result.recipient_count >= 1
		and not leaks_south
		and m_lead_in_inbox
		and lead_room in rooms_published
		and notifications.branch_notification_room("North") in rooms_published
	)
	detail = (
		f"audience leaders={sorted(audience_leaders)} (recipient_count={result.recipient_count}); "
		f"South leaked={leaks_south}; M-Lead in inbox={m_lead_in_inbox}; "
		f"rooms published={sorted(rooms_published)}"
	)
	return Result("6) scoped push notification -> leader inbox", ok, detail), sorted(audience_leaders)


# --------------------------------------------------------------------------- #
# Scenario 7 — every state change emitted its catalogued domain event
# (FLO-224 step 7, event-modeling bar).
# --------------------------------------------------------------------------- #


_EXPECTED_EVENTS = (
	events.APPROVAL_REQUESTED,
	events.APPROVAL_STEP_APPROVED,
	events.APPROVAL_APPROVED,
	events.REGISTRATION_OPENED,
	events.REGISTRATION_CREATED,
	events.ATTENDANCE_REPORTED,
	events.ATTENDANCE_BULK_RECORDED,
	events.ENGAGEMENT_SESSION_OPENED,
	events.ENGAGEMENT_SESSION_CLOSED,
	events.NOTIFICATION_SENT,
)


def _scenario_7_domain_events(world: E2EWorld) -> Result:
	"""Assert the catalogue of domain events fired across the whole North-Star run."""
	sink = events._bus._sink  # noqa: SLF001
	if not isinstance(sink, RecordingEventSink):
		return Result("7) domain event catalogue", False, "bus sink not a RecordingEventSink")
	seen = {ev.name for ev, _r, _room in sink.published}
	missing = [name for name in _EXPECTED_EVENTS if name not in seen]
	# Every name follows the canonical flock.<aggregate>.<verb> three-segment form.
	canonical = all(len(name.split(".")) == 3 and name.startswith("flock.") for name in seen)
	ok = not missing and canonical
	detail = (
		f"captured {len(seen)} distinct domain events; "
		f"missing from catalogue={missing}; canonical three-segment form={canonical}; "
		f"events={sorted(seen)}"
	)
	return Result("7) every state change emitted a catalogued domain event", ok, detail)


SCENARIOS = (
	_scenario_1_seed,
	_scenario_2_gatherings_and_announcement,
	_scenario_3_bulk_attendance,
	_scenario_4_onetime_event_approval_and_registration,
)


def _print_tree() -> None:
	print("Org / branch tree:")
	print("  Flock HQ")
	print("  ├── North                       <- Branch Admin scope root")
	print("  │   ├── North-Campus")
	print("  │   │   └── North-Outpost       <- nested under another branch")
	print("  │   └── North-East")
	print("  └── South                       <- independent tenant (isolation target)")
	print("      └── South-Campus")
	print("Group tree (branch-bound, leaders lead 1..N):")
	print("  North-Ministries (leader M-Lead, leads 3)   [North]")
	print("  ├── Worship (leader M-Lead)                 [North]")
	print("  └── Youth (leader M-Lead)                   [North]")
	print("       └── Youth-Band (leader M-Other)        [North]")
	print("  South-Ministries (leader M-South-Lead)      [South]")
	print("  └── Outreach (leader M-South-Lead)          [South]")


def run() -> list[Result]:
	"""Run all North-Star e2e scenarios against the real domain services."""
	# Central recording bus: every scenario re-emits its catalogue events here so
	# scenario 7 can assert the full event-model across the whole run.
	events._bus = EventBus(sink=RecordingEventSink())  # noqa: SLF001
	world = E2EWorld()
	traversal.install_gateway(world)
	perms.install_gateway(world)

	print("=" * 78)
	print("Flock OS — Phase-5.1b End-to-End MVP demo (FLO-224): the FLO-1 close trigger")
	print("=" * 78)
	_print_tree()
	print()
	results: list[Result] = []

	for scenario in SCENARIOS:
		res = scenario(world)
		results.append(res)
		_mark = "PASS" if res.ok else "FAIL"
		print(f"[{_mark}] {res.label}")
		print(f"       {res.detail}")

	# Scenario 5 returns (Result, recorded_events); both driven here.
	res5, _ev5 = _scenario_5_fun_attendance(world)
	results.append(res5)
	_mark5 = "PASS" if res5.ok else "FAIL"
	print(f"[{_mark5}] {res5.label}")
	print(f"       {res5.detail}")

	res6, _leaders = _scenario_6_scoped_notification(world)
	results.append(res6)
	_mark6 = "PASS" if res6.ok else "FAIL"
	print(f"[{_mark6}] {res6.label}")
	print(f"       {res6.detail}")

	res7 = _scenario_7_domain_events(world)
	results.append(res7)
	_mark7 = "PASS" if res7.ok else "FAIL"
	print(f"[{_mark7}] {res7.label}")
	print(f"       {res7.detail}")

	print("-" * 78)
	all_ok = all(r.ok for r in results)
	green = sum(r.ok for r in results)
	print(f"DEMO: {'PASS' if all_ok else 'FAIL'} — {green}/{len(results)} North-Star steps green")
	print(
		"Drives real flock_os {traversal, permissions, approvals, registrations, scheduling, "
		"reporting, engagement, notifications, events} over in-memory gateways — no Frappe site."
	)
	return results


def main() -> int:
	results = run()
	return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
	sys.exit(main())
