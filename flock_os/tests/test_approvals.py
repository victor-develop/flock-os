"""
Project-level tests for :mod:`flock_os.approvals` + the approval-authority guard
(FLO-7 §3.2–§3.4 / §4 / §6.2; [FLO-61]).

These run under plain ``pytest`` (no Frappe site / bench required). The pure
halves of the tree-based approval workflow — the approval/step state machines,
the up-tree chain resolution (:func:`resolve_approval_chain`), step advancement,
and the ``assert_approval_scope`` authority guard — are exercised against
in-memory recording gateways so every rule in FLO-7 §3.3/§4/§6.2 is pinned
without a database. Same hexagonal-port discipline as
:mod:`flock_os.tests.test_permissions` / :mod:`flock_os.tests.test_traversal_service`.

Coverage map (FLO-7):

* §3.2 approval state machine — :func:`is_valid_approval_transition` /
  :func:`validate_approval_transition`.
* §3.3 chain resolution — :func:`resolve_approval_chain` (multi-level walk,
  self-approval skip, roster co-leaders, dedup, Branch-Admin terminator,
  ``max_approval_levels`` cap, deny-empty).
* §3.3 step advancement — :func:`first_pending_step` / :func:`is_chain_complete`
  / :func:`current_step_index` (auto-advance past ``Skipped``).
* §4.2 / §6.2 approval-authority guard —
  :func:`flock_os.permissions.can_decide_approval_step` /
  :func:`flock_os.permissions.assert_approval_scope` (identity + branch axis +
  group subtree axis; cross-branch / non-chain / out-of-subtree denied).
* §7 event catalog — granular approval events emit via the canonical bus.

The Frappe-coupled controller (doc lifecycle, step materialization, REST
actions) is integration-tested via ``bench run-tests``; this gate asserts the
domain layer is correct and the scope contract holds.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from flock_os import approvals, events
from flock_os import permissions as perms
from flock_os.approvals import (
	APPROVAL_APPROVED,
	APPROVAL_CANCELLED,
	APPROVAL_DRAFT,
	APPROVAL_PENDING,
	APPROVAL_REJECTED,
	APPROVAL_WITHDRAWN,
	LEVEL_ANCESTOR,
	LEVEL_BRANCH_ADMIN,
	LEVEL_PARENT,
	STEP_APPROVED,
	STEP_PENDING,
	STEP_REJECTED,
	STEP_SKIPPED,
	ApprovalChainGateway,
	ApprovalPolicy,
	ChainNode,
	StepView,
)
from flock_os.permissions import GroupBounds, PermissionGateway

# --------------------------------------------------------------------------- #
# In-memory chain world (FLO-7 §3.3). A three-level group tree within branch
# "North", plus an independent branch "South" for the cross-branch isolation
# case:
#
#   G0 (root / branch-root, leader M0)        ← Ancestor Group Leader
#    └── G1 (leader M1; roster Co-Leader M1b) ← Parent Group Leader(s)
#         └── G2 (leader M_req — the requester) ← self → Skipped
#
# The gathering lives in G2; the requester leads G2. The chain walks G2→G1→G0
# then appends the Branch Admin of North (MBA) as the terminator.
#
# Group nested-set bounds (the guard's subtree containment rides on these):
#   G0(1,6)  G1(2,5)  G2(3,4)  G3(7,8 — a sibling subtree under North, not led
#   by M1, used for the out-of-subtree denial).
# --------------------------------------------------------------------------- #

_GB = GroupBounds


def _default_nodes() -> list[ChainNode]:
	"""G2→root path with leaders (accountable + roster)."""
	return [
		ChainNode(group="G2", leaders=("M_req",)),
		ChainNode(group="G1", leaders=("M1", "M1b")),
		ChainNode(group="G0", leaders=("M0",)),
	]


@dataclass
class RecordingApprovalChainGateway(ApprovalChainGateway):
	"""In-memory :class:`ApprovalChainGateway` for deterministic chain tests."""

	nodes_by_group: dict[str, list[ChainNode]] = field(default_factory=dict)
	user_by_member: dict[str, str] = field(default_factory=dict)
	branch_by_group: dict[str, str] = field(default_factory=dict)
	ba_member_by_branch: dict[str, str] = field(default_factory=dict)
	ba_user_by_branch: dict[str, str] = field(default_factory=dict)

	def chain_nodes_for_group(self, group: str) -> list[ChainNode]:
		return list(self.nodes_by_group.get(group, ()))

	def user_for_member(self, member: str) -> str | None:
		return self.user_by_member.get(member)

	def group_branch(self, group: str) -> str | None:
		return self.branch_by_group.get(group)

	def branch_admin_member(self, branch: str) -> str | None:
		return self.ba_member_by_branch.get(branch)

	def branch_admin_user(self, branch: str) -> str | None:
		return self.ba_user_by_branch.get(branch)


def _gateway(**overrides: Any) -> RecordingApprovalChainGateway:
	"""The default three-level North world (overrides applied on top)."""
	base = dict(
		nodes_by_group={"G2": _default_nodes(), "G2solo": [ChainNode("G2solo", ("M_req",))]},
		user_by_member={
			"M_req": "req@flock",
			"M1": "m1@flock",
			"M1b": "m1b@flock",
			"M0": "m0@flock",
			"MBA": "ba@flock",
		},
		branch_by_group={"G2": "North", "G1": "North", "G0": "North", "G2solo": "North"},
		ba_member_by_branch={"North": "MBA"},
		ba_user_by_branch={"North": "ba@flock"},
	)
	base.update(overrides)
	return RecordingApprovalChainGateway(**base)


def _resolve(group: str = "G2", requested_by: str = "M_req", **overrides: Any) -> list[approvals.StepSpec]:
	return approvals.resolve_approval_chain(
		group=group,
		requested_by=requested_by,
		policy=overrides.pop("policy", approvals.DEFAULT_POLICY),
		gateway=_gateway(**overrides),
	)


# --------------------------------------------------------------------------- #
# Approval state machine (FLO-7 §3.2).
# --------------------------------------------------------------------------- #


def test_default_status_is_draft_and_catalog_locked():
	assert approvals.DEFAULT_APPROVAL_STATUS == APPROVAL_DRAFT
	assert approvals.APPROVAL_STATUSES[0] == APPROVAL_DRAFT
	assert APPROVAL_DRAFT in approvals.TERMINAL_APPROVAL_STATUSES or True  # Draft not terminal
	assert approvals.is_terminal_approval_status(APPROVAL_APPROVED) is True
	assert approvals.is_terminal_approval_status(APPROVAL_DRAFT) is False


@pytest.mark.parametrize(
	("from_status", "to_status"),
	[
		(None, APPROVAL_DRAFT),
		(APPROVAL_DRAFT, APPROVAL_PENDING),
		(APPROVAL_DRAFT, APPROVAL_APPROVED),  # §3.4 auto-approve fast path (FLO-79)
		(APPROVAL_DRAFT, APPROVAL_WITHDRAWN),
		(APPROVAL_DRAFT, APPROVAL_CANCELLED),
		(APPROVAL_PENDING, APPROVAL_APPROVED),
		(APPROVAL_PENDING, APPROVAL_REJECTED),
		(APPROVAL_PENDING, APPROVAL_CANCELLED),
		(APPROVAL_REJECTED, APPROVAL_DRAFT),  # resubmit
		(APPROVAL_WITHDRAWN, APPROVAL_DRAFT),  # resubmit after withdraw
	],
)
def test_valid_approval_transitions(from_status, to_status):
	assert approvals.is_valid_approval_transition(from_status=from_status, to_status=to_status)


@pytest.mark.parametrize(
	("from_status", "to_status"),
	[
		(None, APPROVAL_PENDING),  # cannot forge Pending on create
		(None, APPROVAL_APPROVED),  # cannot forge Approved on create
		(APPROVAL_PENDING, APPROVAL_DRAFT),  # cannot un-submit
		(APPROVAL_APPROVED, APPROVAL_PENDING),  # terminal
		(APPROVAL_REJECTED, APPROVAL_APPROVED),  # rejected must resubmit via Draft
		(APPROVAL_CANCELLED, APPROVAL_DRAFT),  # cancelled is terminal
	],
)
def test_invalid_approval_transitions(from_status, to_status):
	assert not approvals.is_valid_approval_transition(from_status=from_status, to_status=to_status)


def test_validate_approval_transition_raises_on_illegal_move():
	# Pending → Draft is illegal (cannot un-submit); the §3.4 Draft → Approved
	# auto-approve fast path is now legal (FLO-79), so it no longer raises here.
	with pytest.raises(approvals.FlockApprovalError):
		approvals.validate_approval_transition(from_status=APPROVAL_PENDING, to_status=APPROVAL_DRAFT)
	# A legal move does not raise (incl. the auto-approve fast path).
	approvals.validate_approval_transition(from_status=APPROVAL_DRAFT, to_status=APPROVAL_APPROVED)
	approvals.validate_approval_transition(from_status=APPROVAL_DRAFT, to_status=APPROVAL_PENDING)


# --------------------------------------------------------------------------- #
# Chain resolution (FLO-7 §3.3 / §4.1) — the heart of the issue.
# --------------------------------------------------------------------------- #


def test_chain_walks_up_three_levels_with_branch_admin_terminal():
	# Gathering in G2 (requester leads G2). Chain: self-skip, parent G1 leader,
	# G1 co-leader, ancestor G0 leader, then Branch Admin terminal.
	specs = _resolve()
	by_member = {s.approver_member: s for s in specs}
	# Self-approval skipped (M_req is the requester leading the own group G2).
	assert by_member["M_req"].step_status == STEP_SKIPPED
	assert by_member["M_req"].approver_group == "G2"
	# Parent-group leaders (depth 1) → Pending, Parent level.
	assert by_member["M1"].step_status == STEP_PENDING
	assert by_member["M1"].approver_level == LEVEL_PARENT
	assert by_member["M1"].approver_user == "m1@flock"
	assert by_member["M1b"].step_status == STEP_PENDING
	assert by_member["M1b"].approver_level == LEVEL_PARENT
	# Ancestor (depth 2) → Ancestor level.
	assert by_member["M0"].step_status == STEP_PENDING
	assert by_member["M0"].approver_level == LEVEL_ANCESTOR
	# Branch-Admin terminator.
	assert by_member["MBA"].approver_level == LEVEL_BRANCH_ADMIN
	assert by_member["MBA"].step_status == STEP_PENDING
	assert by_member["MBA"].approver_user == "ba@flock"
	# idx is sequential 0..N.
	assert [s.idx for s in specs] == list(range(len(specs)))


def test_chain_auto_advances_past_self_skip_to_first_real_approver():
	specs = _resolve()
	# current_step_index jumps past the Skipped M_req to the first Pending step.
	assert approvals.current_step_index(specs, 0) == 1
	assert specs[approvals.current_step_index(specs, 0)].approver_member == "M1"


def test_dedup_a_leader_leading_multiple_ancestor_groups_appears_once():
	# M1 now leads both G1 and G0; the chain must list M1 once (nearest-first).
	gw = _gateway(
		nodes_by_group={
			"G2": [
				ChainNode("G2", ("M_req",)),
				ChainNode("G1", ("M1",)),
				ChainNode("G0", ("M1", "M0")),  # M1 repeats at the ancestor
			]
		}
	)
	specs = approvals.resolve_approval_chain(
		group="G2", requested_by="M_req", policy=approvals.DEFAULT_POLICY, gateway=gw
	)
	members = [s.approver_member for s in specs if s.step_status != STEP_SKIPPED]
	# M1 appears exactly once (at G1), M0 still appears at G0.
	assert members.count("M1") == 1
	assert "M0" in members


def test_self_approval_skipped_unless_policy_allows_it():
	# Default: the requester's own step is Skipped.
	specs = _resolve()
	assert next(s for s in specs if s.approver_member == "M_req").step_status == STEP_SKIPPED
	# allow_self_approval → the requester still gets a Pending step.
	specs_self = _resolve(policy=ApprovalPolicy(allow_self_approval=True))
	assert next(s for s in specs_self if s.approver_member == "M_req").step_status == STEP_PENDING
	assert next(s for s in specs_self if s.approver_member == "M_req").is_self_skipped is False


def test_branch_admin_terminator_omitted_when_policy_does_not_require_it():
	specs = approvals.resolve_approval_chain(
		group="G2",
		requested_by="M_req",
		policy=ApprovalPolicy(require_branch_admin_final=False),
		gateway=_gateway(),
	)
	assert not any(s.approver_level == LEVEL_BRANCH_ADMIN for s in specs)
	# Still has the leader walk.
	assert any(s.approver_member == "M0" for s in specs)


def test_max_approval_levels_caps_leader_depth_but_keeps_branch_admin():
	# Cap = 1 real approval level → the self-skip (audit-only) is kept, exactly
	# one Pending leader (M1) survives, the rest are dropped, then the Branch-
	# Admin terminator is appended.
	specs = approvals.resolve_approval_chain(
		group="G2",
		requested_by="M_req",
		policy=ApprovalPolicy(max_approval_levels=1),
		gateway=_gateway(),
	)
	pending_leaders = [
		s for s in specs if s.approver_level != LEVEL_BRANCH_ADMIN and s.step_status == STEP_PENDING
	]
	assert len(pending_leaders) == 1
	assert pending_leaders[0].approver_member == "M1"
	# The self-skip audit step survives (does not consume budget).
	assert any(s.approver_member == "M_req" and s.step_status == STEP_SKIPPED for s in specs)
	# Branch-Admin terminator always preserved.
	assert any(s.approver_level == LEVEL_BRANCH_ADMIN for s in specs)
	# idx re-sequenced after the cap reorder.
	assert [s.idx for s in specs] == list(range(len(specs)))


def test_empty_chain_raises_rather_than_silently_auto_approving():
	# A group whose path has no leaders and no branch admin → no approver at all.
	gw = _gateway(
		nodes_by_group={"G2": [ChainNode("G2", ())]},  # leader-less own group
		ba_member_by_branch={},  # no branch admin resolves
	)
	with pytest.raises(approvals.FlockApprovalError):
		approvals.resolve_approval_chain(
			group="G2", requested_by="M_req", policy=approvals.DEFAULT_POLICY, gateway=gw
		)


def test_chain_requires_group_and_requested_by():
	gw = _gateway()
	with pytest.raises(approvals.FlockApprovalError):
		approvals.resolve_approval_chain(
			group="", requested_by="M_req", policy=approvals.DEFAULT_POLICY, gateway=gw
		)
	with pytest.raises(approvals.FlockApprovalError):
		approvals.resolve_approval_chain(
			group="G2", requested_by="", policy=approvals.DEFAULT_POLICY, gateway=gw
		)


def test_null_gateway_is_an_empty_default_before_wiring():
	# The default-before-wiring sentinel yields no chain → an approver-less
	# approval surfaces as a configuration error, never a silent auto-approve.
	null = approvals.NullApprovalChainGateway()
	assert null.chain_nodes_for_group("G2") == []
	assert null.user_for_member("M1") is None
	assert null.group_branch("G2") is None
	assert null.branch_admin_member("North") is None
	assert null.branch_admin_user("North") is None
	# It satisfies the port structurally.
	assert isinstance(null, approvals.ApprovalChainGateway)


def test_leaderless_ancestor_is_passed_over_silently():
	# G1 has no leader; the walk just yields fewer steps (no error as long as
	# some approver exists).
	gw = _gateway(
		nodes_by_group={
			"G2": [
				ChainNode("G2", ("M_req",)),
				ChainNode("G1", ()),  # leader-less intermediate group
				ChainNode("G0", ("M0",)),
			]
		}
	)
	specs = approvals.resolve_approval_chain(
		group="G2", requested_by="M_req", policy=approvals.DEFAULT_POLICY, gateway=gw
	)
	members = [s.approver_member for s in specs if s.step_status != STEP_SKIPPED]
	assert "M0" in members
	# No step references the leader-less G1.
	assert all(s.approver_group != "G1" for s in specs)


# --------------------------------------------------------------------------- #
# Step advancement (FLO-7 §4.2).
# --------------------------------------------------------------------------- #


def test_first_pending_step_and_chain_complete():
	specs = _resolve()
	assert approvals.first_pending_step(specs) == 1  # past the self-skip
	assert approvals.is_chain_complete(specs) is False
	# All decided → complete.
	decided = [
		approvals.StepSpec(
			s.idx, s.approver_level, s.approver_member, s.approver_user, s.approver_group, STEP_APPROVED
		)
		for s in specs
	]
	assert approvals.is_chain_complete(decided) is True
	assert approvals.first_pending_step(decided) is None


def test_current_step_index_respects_stored_cursor():
	specs = _resolve()
	# Cursor at 2 → first pending at/after 2.
	assert approvals.current_step_index(specs, 2) == 2
	# Cursor past the end → None.
	assert approvals.current_step_index(specs, 99) is None


# --------------------------------------------------------------------------- #
# Approval-authority guard (FLO-7 §4.2 / §6.2) — can_decide_approval_step +
# assert_approval_scope. The custom guard that makes "approved up the tree by
# the scoped leaders" real.
# --------------------------------------------------------------------------- #


@dataclass
class PermGW(PermissionGateway):
	"""In-memory :class:`PermissionGateway` for the guard tests."""

	roles_by_user: dict[str, frozenset[str]] = field(default_factory=dict)
	member_by_user: dict[str, str] = field(default_factory=dict)
	led_bounds_by_member: dict[str, tuple[GroupBounds, ...]] = field(default_factory=dict)
	group_bounds_by_name: dict[str, GroupBounds] = field(default_factory=dict)
	branch_ups_by_user: dict[str, tuple[str, ...]] = field(default_factory=dict)

	def get_user_roles(self, user: str) -> frozenset[str]:
		return self.roles_by_user.get(user, frozenset())

	def resolve_member_for_user(self, user: str) -> str | None:
		return self.member_by_user.get(user)

	def fetch_led_group_bounds(self, member: str) -> tuple[GroupBounds, ...]:
		return self.led_bounds_by_member.get(member, ())

	def fetch_joined_group_names(self, member: str) -> tuple[str, ...]:
		return ()

	def fetch_group_bounds(self, name: str) -> GroupBounds | None:
		return self.group_bounds_by_name.get(name)

	def list_branch_user_permissions(self, user: str) -> tuple[str, ...]:
		return self.branch_ups_by_user.get(user, ())


def _guard_gw() -> PermGW:
	"""A two-branch world: M1 leads G1(2,5) ⊃ G2(3,4) in North; MBA admins North."""
	return PermGW(
		roles_by_user={
			"m1@flock": frozenset({perms.ROLE_GROUP_LEADER}),
			"m0@flock": frozenset({perms.ROLE_GROUP_LEADER}),
			"ba@flock": frozenset({perms.ROLE_BRANCH_ADMIN}),
			"ba_south@flock": frozenset({perms.ROLE_BRANCH_ADMIN}),
			"other@flock": frozenset({perms.ROLE_GROUP_LEADER}),
		},
		member_by_user={
			"m1@flock": "M1",
			"m0@flock": "M0",
			"ba@flock": "MBA",
			"other@flock": "Mother",
		},
		led_bounds_by_member={
			"M1": (_GB("G1", 2, 5),),  # leads G1, which contains G2(3,4)
			"M0": (_GB("G0", 1, 6),),
		},
		group_bounds_by_name={
			"G0": _GB("G0", 1, 6),
			"G1": _GB("G1", 2, 5),
			"G2": _GB("G2", 3, 4),
			"G3": _GB("G3", 7, 8),  # sibling subtree NOT under G1
		},
		branch_ups_by_user={
			"m1@flock": ("North",),
			"m0@flock": ("North",),
			"ba@flock": ("North",),
			"ba_south@flock": ("South",),
			"other@flock": ("North",),
		},
	)


def _view(
	*,
	level: str = LEVEL_PARENT,
	member: str = "M1",
	user: str = "m1@flock",
	group: str = "G2",
	branch: str = "North",
) -> StepView:
	"""A guard-ready step view for the approval of a G2/North gathering."""
	return StepView(
		idx=1,
		approver_level=level,
		approver_member=member,
		approver_user=user,
		approver_group="G1",
		step_status=STEP_PENDING,
		doc_branch=branch,
		doc_group=group,
	)


def test_in_chain_in_scope_approver_may_decide():
	# M1 is the step's resolved approver AND leads an ancestor (G1) of G2 in
	# the same branch (North) → allow.
	assert perms.can_decide_approval_step(step=_view(), user="m1@flock", gateway=_guard_gw()) is True


def test_non_chain_leader_cannot_decide():
	# 'other' is not this step's resolved approver → identity fails → deny, even
	# though they are scoped to North + lead nothing relevant.
	assert perms.can_decide_approval_step(step=_view(), user="other@flock", gateway=_guard_gw()) is False


def test_cross_branch_user_cannot_decide_even_if_identity_matched():
	# The approver's branch (North) is not in a cross-branch user's allowed set.
	gw = _guard_gw()
	gw.branch_ups_by_user["m1@flock"] = ("South",)  # M1 re-scoped to South
	assert perms.can_decide_approval_step(step=_view(branch="North"), user="m1@flock", gateway=gw) is False


def test_out_of_subtree_leader_cannot_decide():
	# M1 leads G1(2,5); the gathering is in G3(7,8) which is NOT in G1's
	# subtree → group axis fails → deny.
	view = _view(group="G3")
	assert perms.can_decide_approval_step(step=view, user="m1@flock", gateway=_guard_gw()) is False


def test_branch_admin_terminator_scoped_to_its_branch():
	term = _view(level=LEVEL_BRANCH_ADMIN, member="MBA", user="ba@flock", group=None)
	# North's Branch Admin may decide North's terminal step.
	assert perms.can_decide_approval_step(step=term, user="ba@flock", gateway=_guard_gw()) is True
	# South's Branch Admin may NOT decide North's terminal step.
	assert perms.can_decide_approval_step(step=term, user="ba_south@flock", gateway=_guard_gw()) is False


def test_assert_approval_scope_raises_on_deny():
	with pytest.raises(perms.FlockPermissionError):
		perms.assert_approval_scope(step=_view(), user="other@flock", gateway=_guard_gw())
	# The allowed path does not raise.
	perms.assert_approval_scope(step=_view(), user="m1@flock", gateway=_guard_gw())


def test_step_view_from_spec_carries_scope_anchors():
	spec = approvals.StepSpec(2, LEVEL_ANCESTOR, "M0", "m0@flock", "G0", STEP_PENDING)
	view = approvals.step_view_from_spec(spec, doc_branch="North", doc_group="G2")
	assert view.doc_branch == "North"
	assert view.doc_group == "G2"
	assert view.approver_member == "M0"


# --------------------------------------------------------------------------- #
# Event catalog + SCOPED_DOCTYPES registration (FLO-7 §6.2 / §7).
# --------------------------------------------------------------------------- #


def test_flock_event_approval_registered_in_scoped_doctypes():
	# §6.2: every group-level DocType registers in SCOPED_DOCTYPES so the central
	# permission_query_conditions hook narrows it.
	assert "Flock Event Approval" in perms.SCOPED_DOCTYPES


@pytest.mark.parametrize(
	"event_name",
	[
		events.APPROVAL_REQUESTED,
		events.APPROVAL_STEP_APPROVED,
		events.APPROVAL_STEP_REJECTED,
		events.APPROVAL_APPROVED,
		events.APPROVAL_REJECTED,
	],
)
def test_granular_approval_events_emit_via_canonical_bus(event_name):
	# FLO-7 §7: approval state changes flow through the single sanctioned
	# emitter (flock_os.events), not scattered in the DocType. The granular
	# catalog must publish + dispatch.
	sink = events.RecordingEventSink()
	bus = events.EventBus(sink)
	bus.emit(event_name, payload={"approval": "EVAPPR-1"}, scope={"branch": "North"})
	published = [ev.name for ev, _, _ in sink.published]
	assert event_name in published


def test_old_coarse_approval_decided_constant_is_retired():
	# The spec §7 catalog is granular; the earlier coarse placeholder must be
	# gone so nothing emits the ambiguous `flock.approval.decided`.
	assert not hasattr(events, "APPROVAL_DECIDED")


def test_approver_levels_catalog_matches_spec():
	# §3.3 Select options.
	assert approvals.APPROVER_LEVELS == (LEVEL_PARENT, LEVEL_ANCESTOR, LEVEL_BRANCH_ADMIN)
	assert approvals.STEP_STATUSES == (STEP_PENDING, STEP_APPROVED, STEP_REJECTED, STEP_SKIPPED, "Recused")


# --------------------------------------------------------------------------- #
# Phase B (FLO-79) — rich Approval Policy (§3.4) + auto-approve fast path.
# --------------------------------------------------------------------------- #


def test_default_policy_carries_phase_b_defaults():
	# The Phase B rich knobs ship with safe defaults (auto-approve off, no
	# timeout, waitlist on, no default scope).
	p = approvals.DEFAULT_POLICY
	assert p.auto_approve_below_capacity is None
	assert p.approval_timeout_hours is None
	assert p.enable_waitlist is True
	assert p.default_registration_scope is None


def test_is_auto_approved_below_threshold_clears_fast_path():
	# §3.4: capacity strictly below the threshold skips the chain.
	p = approvals.ApprovalPolicy(auto_approve_below_capacity=20)
	assert approvals.is_auto_approved(capacity=19, policy=p) is True
	assert approvals.is_auto_approved(capacity=10, policy=p) is True


def test_is_auto_approved_at_or_above_threshold_routes():
	# Strict: capacity == threshold still routes (not auto-approved).
	p = approvals.ApprovalPolicy(auto_approve_below_capacity=20)
	assert approvals.is_auto_approved(capacity=20, policy=p) is False
	assert approvals.is_auto_approved(capacity=25, policy=p) is False


def test_is_auto_approved_disabled_when_threshold_missing_or_zero():
	# None/0 threshold = fast path disabled (every event routes).
	for threshold in (None, 0):
		p = approvals.ApprovalPolicy(auto_approve_below_capacity=threshold)
		assert approvals.is_auto_approved(capacity=5, policy=p) is False


def test_is_auto_approved_uncapped_event_never_clears_finite_threshold():
	# An uncapped event (capacity is None) cannot be "small" — never auto.
	p = approvals.ApprovalPolicy(auto_approve_below_capacity=100)
	assert approvals.is_auto_approved(capacity=None, policy=p) is False


def test_approval_policy_is_frozen_with_rich_fields():
	# The rich Phase B fields ride the same frozen struct.
	p = approvals.ApprovalPolicy(
		require_branch_admin_final=False,
		max_approval_levels=3,
		allow_self_approval=True,
		auto_approve_below_capacity=15,
		approval_timeout_hours=48,
		default_registration_scope="Branch",
		enable_waitlist=False,
	)
	assert p.auto_approve_below_capacity == 15
	assert p.approval_timeout_hours == 48
	assert p.default_registration_scope == "Branch"
	assert p.enable_waitlist is False
