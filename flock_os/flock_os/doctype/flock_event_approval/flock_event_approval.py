# Copyright (c) 2026, Flock OS and contributors
# For license information, please see license.txt

from __future__ import annotations

import frappe
from frappe.model.document import Document

from flock_os import approvals, events, permissions
from flock_os.traversal import get_service as get_traversal_service

# The leadership roster roles that form a group's approver set (ADR §4.3 /
# FLO-7 §3.3 #2) — the accountable ``Flock Group.leader`` plus the roster
# ``Leader``/``Co-Leader`` edges. Mirrors ``permissions.LEADER_ROSTER_ROLES``.
_LEADER_ROLES = ("Leader", "Co-Leader")


class FlockEventApproval(Document):
	# Flock Event Approval = the one-time-event approval state machine + chain
	# (FLO-7 §3.2). Submittable (§3.2): Frappe docstatus overlays open/locked/
	# voided; the domain ``status`` field overlays the approval lifecycle
	# (Draft -> Pending Approval -> {Approved | Rejected | Cancelled |
	# Withdrawn}). 1:1 with a ``Flock Gathering`` (event_category = One-time).
	#
	# The pure invariants (state machine, branch/group binding) live in
	# :mod:`flock_os.approvals`; this controller enforces them on save. The
	# approval *actions* (submit / approve / reject / withdraw / cancel) are the
	# scoped ``@frappe.whitelist()`` functions below — they run the pure
	# transitions, materialize the chain, enforce ``assert_approval_scope``, and
	# emit the canonical approval events (FLO-7 §7). Row-level reads are scoped
	# by the central ``permission_query_conditions`` hook (registered in
	# ``flock_os.permissions.SCOPED_DOCTYPES``).

	def validate(self):
		self._denormalize_tenant_floor()
		self._validate_branch_group_binding()
		self._validate_status_transition()
		self._validate_requested_by_leads_group()

	# ------------------------------------------------------------------ #
	# Scoping contract (ADR §3 / §4.2) — mirrors Flock Gathering.
	# ------------------------------------------------------------------ #
	def _denormalize_tenant_floor(self):
		if self.organization:
			return
		if self.branch:
			branch_org = frappe.db.get_value("Flock Branch", self.branch, "organization")
			if branch_org:
				self.organization = branch_org

	def _validate_branch_group_binding(self):
		# ADR §4.2: the approval is branch-bound to its group (the group subtree
		# is branch-bound), and must match its gathering's branch/group — the
		# approval carries the gathering's exact scope anchors so the row-level
		# hooks + the approval-authority guard resolve against the same subtree.
		group_branch = frappe.db.get_value("Flock Group", self.group, "branch") if self.group else None
		if not self.branch:
			frappe.throw(
				"Flock Event Approval.branch is required (the row-level perm anchor).", frappe.ValidationError
			)
		if group_branch is None:
			frappe.throw(
				"Flock Event Approval.group has no branch — cannot bind the approval.", frappe.ValidationError
			)
		if group_branch != self.branch:
			frappe.throw(
				f"An approval's branch must match its group's branch (group branch {group_branch!r} "
				f"!= approval branch {self.branch!r}). ADR §4.2.",
				frappe.ValidationError,
			)
		# Keep the approval's scope anchored to its gathering (the gathering is
		# the entity being approved); a mismatch would let an approval ride a
		# different subtree than the event it authorizes.
		gathering_branch, gathering_group = frappe.db.get_value(
			"Flock Gathering", self.gathering, ["branch", "group"]
		) or (None, None)
		if gathering_branch and (gathering_branch != self.branch or gathering_group != self.group):
			frappe.throw(
				"An approval's branch/group must match its gathering's branch/group (FLO-7 §3.2).",
				frappe.ValidationError,
			)

	def _validate_status_transition(self):
		before = self.get_doc_before_save()
		from_status = before.status if before is not None else None
		try:
			approvals.validate_approval_transition(from_status=from_status, to_status=self.status)
		except approvals.FlockApprovalError as exc:
			frappe.throw(str(exc), frappe.ValidationError)

	def _validate_requested_by_leads_group(self):
		# FLO-7 §4.1: the requester must be an active leader of the gathering's
		# group (accountable leader or a roster Leader/Co-Leader). This is the
		# "leader-created" precondition — a non-leader cannot start a chain.
		if not self.requested_by or not self.group:
			frappe.throw("Requested By and Group are required (FLO-7 §4.1).", frappe.ValidationError)
		accountable = frappe.db.get_value("Flock Group", self.group, "leader")
		on_roster = bool(
			frappe.db.exists(
				"Flock Group Member",
				{"group": self.group, "member": self.requested_by, "role": ["in", list(_LEADER_ROLES)]},
			)
		)
		if self.requested_by != accountable and not on_roster:
			frappe.throw(
				f"Requested By {self.requested_by!r} is not a leader of group {self.group!r} (FLO-7 §4.1).",
				frappe.ValidationError,
			)


# ---------------------------------------------------------------------------- #
# Frappe approval-chain gateway adapter (ADR-0001 §2 hexagonal boundary).
#
# Implements :class:`flock_os.approvals.ApprovalChainGateway` over Frappe reads.
# The group→root path + accountable leaders come from the traversal service
# (FLO-19) — DRY, no copied tree logic; the roster co-leader axis is the only
# approval-specific read. Lazy Frappe use only (this whole module is Frappe's
# natural home and coverage-omitted under the gate).
# ---------------------------------------------------------------------------- #
class FrappeApprovalChainGateway:
	"""Production adapter over the traversal service + roster reads (FLO-7 §3.3)."""

	@property
	def _traversal(self):
		return get_traversal_service()

	@property
	def _frappe(self):
		return frappe

	def chain_nodes_for_group(self, group: str) -> list[approvals.ChainNode]:
		# The path group→root (inclusive), each row carrying its accountable
		# ``leader`` (FLO-19). For each group, union in the roster co-leaders so
		# a group led jointly by two co-leaders produces two steps, not one.
		path_rows = self._traversal.group_path_to_root(group)
		nodes: list[approvals.ChainNode] = []
		for row in path_rows:
			group_name = row.get("name")
			if not group_name:
				continue
			leaders: list[str] = []
			accountable = row.get("leader")
			if accountable:
				leaders.append(accountable)
			roster = self._frappe.get_all(
				"Flock Group Member",
				filters={"group": group_name, "role": ["in", list(_LEADER_ROLES)]},
				pluck="member",
			)
			for member in roster:
				if member and member not in leaders:
					leaders.append(member)
			if leaders:
				nodes.append(approvals.ChainNode(group=group_name, leaders=tuple(leaders)))
		return nodes

	def user_for_member(self, member: str) -> str | None:
		if not member:
			return None
		return self._frappe.db.get_value("Flock Member", member, "linked_user")

	def group_branch(self, group: str) -> str | None:
		if not group:
			return None
		return self._frappe.db.get_value("Flock Group", group, "branch")

	def branch_admin_member(self, branch: str) -> str | None:
		# The Branch Admin for ``branch``: the Flock Member whose linked user
		# holds the Flock Branch Admin role AND a Flock Branch User Permission
		# for this branch (the terminal-step approver, FLO-7 §3.3 #4).
		if not branch:
			return None
		admin_users = self._frappe.get_all(
			"Has Role",
			filters={"role": permissions.ROLE_BRANCH_ADMIN, "parenttype": "User"},
			pluck="parent",
		)
		for user in admin_users:
			allowed = self._frappe.get_all(
				"User Permission",
				filters={"user": user, "allow": permissions.BRANCH_DOCTYPE},
				pluck="for_value",
			)
			if branch in allowed:
				member = self._frappe.db.get_value("Flock Member", {"linked_user": user}, "name")
				if member:
					return member
		return None

	def branch_admin_user(self, branch: str) -> str | None:
		member = self.branch_admin_member(branch)
		return self.user_for_member(member) if member else None


def _approval_policy_from_row(policy_name: str | None) -> approvals.ApprovalPolicy:
	"""Build the frozen :class:`ApprovalPolicy` from a policy row (§3.4).

	The nearest policy to the gathering's branch wins; absent one, the default
	(Branch Admin terminal, self-approval off, depth uncapped, auto-approve/
	timeout disabled, waitlist on) applies. Phase B ([FLO-79]) maps the full
	rich §3.4 knob set so chain resolution + the submit fast path see the same
	resolved struct.
	"""
	if not policy_name:
		return approvals.DEFAULT_POLICY
	row = (
		frappe.db.get_value(
			"Flock Event Approval Policy",
			policy_name,
			[
				"require_branch_admin_final",
				"max_approval_levels",
				"allow_self_approval",
				"auto_approve_below_capacity",
				"approval_timeout_hours",
				"default_registration_scope",
				"enable_waitlist",
			],
			as_dict=True,
		)
		or {}
	)
	max_levels = row.get("max_approval_levels")
	auto_cap = row.get("auto_approve_below_capacity")
	timeout = row.get("approval_timeout_hours")
	return approvals.ApprovalPolicy(
		require_branch_admin_final=bool(row.get("require_branch_admin_final", 1)),
		max_approval_levels=int(max_levels) if max_levels not in (None, "") else None,
		allow_self_approval=bool(row.get("allow_self_approval", 0)),
		auto_approve_below_capacity=int(auto_cap) if auto_cap not in (None, "") else None,
		approval_timeout_hours=int(timeout) if timeout not in (None, "") else None,
		default_registration_scope=row.get("default_registration_scope") or None,
		enable_waitlist=bool(row.get("enable_waitlist", 1)),
	)


def _resolve_chain_for(
	approval: FlockEventApproval, *, policy: approvals.ApprovalPolicy | None = None
) -> list[approvals.StepSpec]:
	"""Resolve the approval chain for ``approval`` via the Frappe gateway (§4.1).

	``policy`` is optional only to preserve the old call shape; the submit path
	passes the already-resolved policy so the row is read once (DRY).
	"""
	return approvals.resolve_approval_chain(
		group=approval.group,
		requested_by=approval.requested_by,
		policy=policy or _approval_policy_from_row(approval.approval_policy),
		gateway=FrappeApprovalChainGateway(),
	)


def _materialize_steps(approval: FlockEventApproval, specs: list[approvals.StepSpec]) -> None:
	"""Replace ``approval.steps`` with rows materialized from ``specs`` (§3.3)."""
	approval.set("steps", [])
	for spec in specs:
		approval.append(
			"steps",
			{
				"approver_level": spec.approver_level,
				"approver_member": spec.approver_member,
				"approver_user": spec.approver_user,
				"approver_group": spec.approver_group,
				"step_status": spec.step_status,
			},
		)


def _step_view(approval: FlockEventApproval, idx: int) -> approvals.StepView:
	"""Lift the materialized step at ``idx`` into a guard-ready :class:`StepView`."""
	step = approval.steps[idx]
	return approvals.StepView(
		idx=idx,
		approver_level=step.approver_level,
		approver_member=step.approver_member,
		approver_user=step.approver_user,
		approver_group=step.approver_group,
		step_status=step.step_status,
		doc_branch=approval.branch,
		doc_group=approval.group,
	)


def _scope_for_event(approval: FlockEventApproval) -> dict:
	"""Row-level scope anchors carried on every approval event (FLO-7 §7)."""
	return {"branch": approval.branch, "group": approval.group, "organization": approval.organization}


def _sync_gathering_status(approval: FlockEventApproval, gathering_status: str) -> None:
	"""Write back the denormalized approval status to the gathering (§3.1 / §4.2)."""
	frappe.db.set_value(
		"Flock Gathering",
		approval.gathering,
		{"approval_status": gathering_status, "approval_request": approval.name},
	)


def _open_registration_on_final_approval(approval: FlockEventApproval) -> None:
	"""On final approval, confirm the registration scope on the gathering (§4.2 #2).

	The leader's ``proposed_registration_scope`` is copied to the gathering's
	``registration_scope`` so the eligibility gate + the ``registration.opened``
	event fire on the confirmed scope. Emits ``flock.registration.opened`` via
	the canonical emitter (FLO-7 §7). The window itself (opens_on/closes_on +
	capacity) is set by the leader at propose time and confirmed implicitly here
	— the gathering is now Approved + scoped, so ``is_registration_window_open``
	can read True once the window bounds are met.
	"""
	confirmed_scope = approval.proposed_registration_scope or "None"
	frappe.db.set_value("Flock Gathering", approval.gathering, "registration_scope", confirmed_scope)
	events.emit(
		events.REGISTRATION_OPENED,
		payload={
			"approval": approval.name,
			"gathering": approval.gathering,
			"scope": confirmed_scope,
		},
		scope=_scope_for_event(approval),
	)


# ---------------------------------------------------------------------------- #
# Approval actions (FLO-7 §4 / §8) — scoped ``@frappe.whitelist()`` endpoints.
#
# Each enforces the pure transition, the scope guard, and emits the canonical
# approval event. The session user is the actor for scope + audit.
# ---------------------------------------------------------------------------- #
@frappe.whitelist()
def preview_approval_chain(group: str, requested_by: str | None = None) -> list[dict]:
	"""GET ``?group=<name>[&requested_by=<member>]`` → the resolved chain (UI pre-submit, §8).

	Resolves the chain without persisting, so a leader can preview who will
	approve before submitting. ``requested_by`` defaults to the session user's
	linked member so self-approval skips render correctly.
	"""
	if not group:
		frappe.throw("group is required")
	if not requested_by:
		requested_by = frappe.db.get_value("Flock Member", {"linked_user": frappe.session.user}, "name")
	specs = approvals.resolve_approval_chain(
		group=group,
		requested_by=requested_by or "",
		policy=approvals.DEFAULT_POLICY,
		gateway=FrappeApprovalChainGateway(),
	)
	return [
		{
			"idx": s.idx,
			"approver_level": s.approver_level,
			"approver_member": s.approver_member,
			"approver_user": s.approver_user,
			"approver_group": s.approver_group,
			"step_status": s.step_status,
		}
		for s in specs
	]


@frappe.whitelist()
def submit_for_approval(approval_id: str) -> dict:
	"""Materialize the chain + move the request to ``Pending Approval`` (§4.1),
	or land ``Approved`` directly via the §3.4 auto-approve fast path.

	Only the requester may submit. For events under the policy's
	``auto_approve_below_capacity`` threshold (§3.4), the chain is skipped — the
	request lands terminal ``Approved`` with ``auto_approved=1`` and the
	terminal ``flock.approval.approved`` event carries the ``auto_approved``
	marker. Otherwise the chain is materialized and ``flock.approval.requested``
	is emitted. The policy's ``approval_timeout_hours`` is denormalized onto the
	approval row so the FLO-8 reminder/escalation scheduler reads it without
	re-querying the policy (§3.4).
	"""
	approval: FlockEventApproval = frappe.get_doc("Flock Event Approval", approval_id)
	if approval.requested_by != frappe.db.get_value(
		"Flock Member", {"linked_user": frappe.session.user}, "name"
	):
		# The requester owns the submit; an approver cannot pre-submit someone
		# else's request.
		frappe.throw(
			"Only the requester may submit an approval for approval (FLO-7 §4.1).", frappe.PermissionError
		)
	policy = _approval_policy_from_row(approval.approval_policy)
	# Denormalize the timeout so FLO-8's scheduler reads it off the row (§3.4).
	if policy.approval_timeout_hours is not None:
		approval.timeout_hours = policy.approval_timeout_hours
	approval.requested_at = frappe.utils.now()

	# §3.4 small-group fast path: capacity < threshold skips the chain.
	capacity = frappe.db.get_value("Flock Gathering", approval.gathering, "capacity")
	capacity_int = int(capacity) if capacity not in (None, "") else None
	if approvals.is_auto_approved(capacity=capacity_int, policy=policy):
		approvals.validate_approval_transition(
			from_status=approval.status, to_status=approvals.APPROVAL_APPROVED
		)
		approval.status = approvals.APPROVAL_APPROVED
		approval.auto_approved = 1
		approval.final_decision_by = frappe.session.user
		approval.final_decision_at = frappe.utils.now()
		approval.save(ignore_permissions=True)
		_sync_gathering_status(approval, approvals.APPROVAL_APPROVED)
		_open_registration_on_final_approval(approval)
		events.emit(
			events.APPROVAL_APPROVED,
			payload={
				"approval": approval.name,
				"gathering": approval.gathering,
				"scope": approval.proposed_registration_scope,
				"auto_approved": True,
			},
			scope=_scope_for_event(approval),
		)
		return {"approval": approval.name, "status": approval.status, "auto_approved": True}

	# Standard chain path (§4.1).
	approvals.validate_approval_transition(from_status=approval.status, to_status=approvals.APPROVAL_PENDING)
	specs = _resolve_chain_for(approval, policy=policy)
	_materialize_steps(approval, specs)
	approval.status = approvals.APPROVAL_PENDING
	approval.current_step = approvals.current_step_index(specs) or 0
	approval.save(ignore_permissions=True)
	_sync_gathering_status(approval, approvals.APPROVAL_PENDING)
	events.emit(
		events.APPROVAL_REQUESTED,
		payload={
			"approval": approval.name,
			"gathering": approval.gathering,
			"requested_by": approval.requested_by,
		},
		scope=_scope_for_event(approval),
	)
	return {"approval": approval.name, "status": approval.status, "current_step": approval.current_step}


def _decide_current_step(approval_id: str, *, decide: str) -> FlockEventApproval:
	"""Load the approval + assert the session user may decide its current step (§4.2)."""
	approval: FlockEventApproval = frappe.get_doc("Flock Event Approval", approval_id)
	if approval.status != approvals.APPROVAL_PENDING:
		frappe.throw(
			f"Approval {approval_id!r} is {approval.status!r}; only a Pending request can be decided.",
			frappe.ValidationError,
			title="Approval not pending",
		)
	idx = approvals.current_step_index(approval.steps, approval.current_step)
	if idx is None:
		frappe.throw("No pending step remains on this approval (FLO-7 §4.2).", frappe.ValidationError)
	view = _step_view(approval, idx)
	# The single sanctioned approval-authority guard (FLO-7 §6.2): caller must
	# be this step's resolved approver AND in-scope (both axes).
	permissions.assert_approval_scope(step=view, user=frappe.session.user, gateway=permissions.get_gateway())
	return approval


@frappe.whitelist()
def approve_event(approval_id: str, comment: str | None = None) -> dict:
	"""Approve the current step; advance or finalize (§4.2). Emits step + terminal events."""
	approval = _decide_current_step(approval_id, decide=approvals.STEP_APPROVED)
	idx = approvals.current_step_index(approval.steps, approval.current_step) or 0
	step = approval.steps[idx]
	step.step_status = approvals.STEP_APPROVED
	step.decided_by = frappe.session.user
	step.decided_at = frappe.utils.now()
	step.comment = comment or ""
	events.emit(
		events.APPROVAL_STEP_APPROVED,
		payload={"approval": approval.name, "step": idx, "approver": frappe.session.user},
		scope=_scope_for_event(approval),
	)

	next_idx = approvals.current_step_index(approval.steps, approval.current_step)
	if next_idx is None:
		# Final approval: terminal state + gathering write-back (§4.2).
		approval.status = approvals.APPROVAL_APPROVED
		approval.final_decision_by = frappe.session.user
		approval.final_decision_at = frappe.utils.now()
		approval.save(ignore_permissions=True)
		_sync_gathering_status(approval, approvals.APPROVAL_APPROVED)
		# §4.2 #2: confirm the registration scope + open registration. Emits
		# ``flock.registration.opened`` (the registration gate FLO-62 owns).
		_open_registration_on_final_approval(approval)
		events.emit(
			events.APPROVAL_APPROVED,
			payload={
				"approval": approval.name,
				"gathering": approval.gathering,
				"scope": approval.proposed_registration_scope,
			},
			scope=_scope_for_event(approval),
		)
		return {"approval": approval.name, "status": approval.status, "finalized": True}
	approval.current_step = next_idx
	approval.save(ignore_permissions=True)
	return {"approval": approval.name, "status": approval.status, "current_step": approval.current_step}


@frappe.whitelist()
def reject_event(approval_id: str, reason: str) -> dict:
	"""Reject at the current step → ``Rejected`` (§4.3). Emits step + terminal events."""
	if not reason:
		frappe.throw("A rejection reason is required (FLO-7 §4.3).", frappe.ValidationError)
	approval = _decide_current_step(approval_id, decide=approvals.STEP_REJECTED)
	idx = approvals.current_step_index(approval.steps, approval.current_step) or 0
	step = approval.steps[idx]
	step.step_status = approvals.STEP_REJECTED
	step.decided_by = frappe.session.user
	step.decided_at = frappe.utils.now()
	step.comment = reason
	approval.status = approvals.APPROVAL_REJECTED
	approval.rejection_reason = reason
	approval.final_decision_by = frappe.session.user
	approval.final_decision_at = frappe.utils.now()
	approval.save(ignore_permissions=True)
	_sync_gathering_status(approval, approvals.APPROVAL_REJECTED)
	events.emit(
		events.APPROVAL_STEP_REJECTED,
		payload={"approval": approval.name, "step": idx, "approver": frappe.session.user, "reason": reason},
		scope=_scope_for_event(approval),
	)
	events.emit(
		events.APPROVAL_REJECTED,
		payload={"approval": approval.name, "gathering": approval.gathering, "reason": reason},
		scope=_scope_for_event(approval),
	)
	return {"approval": approval.name, "status": approval.status}


@frappe.whitelist()
def withdraw_event_request(approval_id: str) -> dict:
	"""Requester withdraws a Draft/Pending request → ``Withdrawn`` (§4.4). Terminal."""
	approval: FlockEventApproval = frappe.get_doc("Flock Event Approval", approval_id)
	if approval.status not in (approvals.APPROVAL_DRAFT, approvals.APPROVAL_PENDING):
		frappe.throw(
			f"Approval {approval_id!r} is {approval.status!r}; only Draft/Pending can be withdrawn.",
			frappe.ValidationError,
			title="Cannot withdraw",
		)
	requester = frappe.db.get_value("Flock Member", {"linked_user": frappe.session.user}, "name")
	if approval.requested_by != requester:
		frappe.throw("Only the requester may withdraw an approval (FLO-7 §4.4).", frappe.PermissionError)
	approval.status = approvals.APPROVAL_WITHDRAWN
	approval.save(ignore_permissions=True)
	_sync_gathering_status(approval, approvals.APPROVAL_WITHDRAWN)
	return {"approval": approval.name, "status": approval.status}


@frappe.whitelist()
def cancel_event_request(approval_id: str) -> dict:
	"""Admin cancels a request → ``Cancelled`` (§4.4). Terminal; preserves audit."""
	approval: FlockEventApproval = frappe.get_doc("Flock Event Approval", approval_id)
	roles = set(frappe.get_roles(frappe.session.user))
	if not (roles & {permissions.ROLE_ORG_ADMIN, permissions.ROLE_BRANCH_ADMIN}):
		frappe.throw("Only an Org/Branch Admin may cancel an approval (FLO-7 §4.4).", frappe.PermissionError)
	approvals.validate_approval_transition(
		from_status=approval.status, to_status=approvals.APPROVAL_CANCELLED
	)
	approval.status = approvals.APPROVAL_CANCELLED
	approval.save(ignore_permissions=True)
	_sync_gathering_status(approval, approvals.APPROVAL_CANCELLED)
	return {"approval": approval.name, "status": approval.status}
