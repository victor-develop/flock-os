"""
Tree-based approval workflow — domain layer (FLO-7 §3.2–§3.4 / §4; [FLO-61]).

This is the **pure** invariants half of the one-time-event approval workflow.
A one-time event is a ``Flock Gathering`` (``event_category = One-time``); its
approval is routed **up the reporting tree** by the scoped branch/group leaders
(ADR-0001 §6.6, FLO-5 §4.6). This module owns three things, kept framework-
agnostic (no Frappe import) so they run under plain ``pytest`` — the same
hexagonal discipline as :mod:`flock_os.gatherings` /
:mod:`flock_os.permissions`:

* **Approval + step state machines** (§4) — ``Draft → Pending Approval →
  {Approved | Rejected | Cancelled | Withdrawn}``, with a ``Rejected → Draft``
  resubmit path. Each :class:`StepSpec` carries its own ``Pending → {Approved |
  Rejected | Skipped | Recused}`` lifecycle.
* **Chain resolution** (:func:`resolve_approval_chain`) — walks ``parent_group``
  from the gathering's group up to the branch-root, materializing one approver
  step per unique leader nearest-first, optionally appending the Branch Admin as
  the terminal approver. Self-approval is skipped; the chain is deduplicated;
  ``max_approval_levels`` caps depth. The tree/roster reads come from the
  :class:`ApprovalChainGateway` port (production: :class:`FrappeApprovalChainGateway`;
  tests: :class:`RecordingApprovalChainGateway`).
* **Step advancement** — :func:`first_pending_step` / :func:`is_chain_complete`
  so the controller can advance ``current_step`` and decide final approval.

This module is **pure** (no Frappe import) — the same posture as
:mod:`flock_os.gatherings`. The Frappe-coupled half lives elsewhere:

* the DocType controller (``flock_event_approval.py``) owns the doc lifecycle,
  the step materialization, the gathering write-back, the
  :class:`FrappeApprovalChainGateway` adapter, and the ``@frappe.whitelist()``
  REST actions (FLO-7 §8) — Frappe's natural home, and coverage-omitted;
* :mod:`flock_os.permissions` owns the ``assert_approval_scope`` guard
  (FLO-7 §6.2) — the single sanctioned approval-authority check;
* :mod:`flock_os.events` is the canonical event publisher (FLO-7 §7) — approval
  state changes emit there, never scattered in the DocType.

Layering (ADR-0001 §2 separation of concerns)::

    @frappe.whitelist() actions            (doctype controller, transport)
      -> resolve_approval_chain(...)        (THIS module, pure domain)
      |   |-> ApprovalChainGateway port     (tree/roster reads, hexagonal)
      -> flock_os.permissions.assert_approval_scope   (the scope guard)
      -> flock_os.events.emit(...)          (canonical event publisher)
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Protocol, runtime_checkable

# ---------------------------------------------------------------------------- #
# Approval status catalog (FLO-7 §3.2 state machine). The canonical ``Select``
# options for ``Flock Event Approval.status``. Kept here (not in fixtures)
# because the transition table below keys on the exact strings — the catalog +
# the transition table must stay a single source of truth, exactly like
# :mod:`flock_os.gatherings`.
# ---------------------------------------------------------------------------- #
APPROVAL_DRAFT = "Draft"
APPROVAL_PENDING = "Pending Approval"
APPROVAL_APPROVED = "Approved"
APPROVAL_REJECTED = "Rejected"
APPROVAL_CANCELLED = "Cancelled"
APPROVAL_WITHDRAWN = "Withdrawn"

#: The ordered ``Select`` options for ``Flock Event Approval.status`` (§3.2).
APPROVAL_STATUSES: tuple[str, ...] = (
	APPROVAL_DRAFT,
	APPROVAL_PENDING,
	APPROVAL_APPROVED,
	APPROVAL_REJECTED,
	APPROVAL_CANCELLED,
	APPROVAL_WITHDRAWN,
)

#: Default status for a newly created approval request (§3.2 — ``Draft``).
DEFAULT_APPROVAL_STATUS = APPROVAL_DRAFT

#: Statuses from which no further approval transition is allowed (§3.2).
TERMINAL_APPROVAL_STATUSES: frozenset[str] = frozenset(
	{APPROVAL_APPROVED, APPROVAL_REJECTED, APPROVAL_CANCELLED, APPROVAL_WITHDRAWN}
)

# ---------------------------------------------------------------------------- #
# Approval-step status catalog (FLO-7 §3.3). One ``Flock Event Approval Step``
# row per approver in the resolved chain.
# ---------------------------------------------------------------------------- #
STEP_PENDING = "Pending"
STEP_APPROVED = "Approved"
STEP_REJECTED = "Rejected"
STEP_SKIPPED = "Skipped"
STEP_RECUSED = "Recused"

#: The ordered ``Select`` options for ``Flock Event Approval Step.step_status``.
STEP_STATUSES: tuple[str, ...] = (STEP_PENDING, STEP_APPROVED, STEP_REJECTED, STEP_SKIPPED, STEP_RECUSED)

#: Step statuses that have reached a decision (no longer awaiting action). A
#: ``Skipped`` step (self-approval) counts as decided so the chain auto-advances.
STEP_DECIDED: frozenset[str] = frozenset({STEP_APPROVED, STEP_REJECTED, STEP_SKIPPED, STEP_RECUSED})

# ---------------------------------------------------------------------------- #
# Approver-level labels (FLO-7 §3.3). Label each step by the relationship of its
# group to the gathering's group — drives UI grouping + audit clarity.
# ---------------------------------------------------------------------------- #
LEVEL_PARENT = "Parent Group Leader"
LEVEL_ANCESTOR = "Ancestor Group Leader"
LEVEL_BRANCH_ADMIN = "Branch Admin"

#: The ``approver_level`` ``Select`` options (§3.3).
APPROVER_LEVELS: tuple[str, ...] = (LEVEL_PARENT, LEVEL_ANCESTOR, LEVEL_BRANCH_ADMIN)


class FlockApprovalError(ValueError):
	"""Raised when an approval invariant (state machine / chain) is violated."""


# ---------------------------------------------------------------------------- #
# Approval state machine (FLO-7 §3.2 diagram).
#
# Draft → Pending Approval (leader submits); Pending → {Approved | Rejected |
# Cancelled}; Rejected/Withdrawn → Draft (resubmit, decided steps preserved for
# audit). Approved/Cancelled are terminal.
# ---------------------------------------------------------------------------- #
#: Legal ``from -> {to, ...}`` approval transitions (§3.2 diagram).
APPROVAL_TRANSITIONS: dict[str, frozenset[str]] = {
	APPROVAL_DRAFT: frozenset({APPROVAL_PENDING, APPROVAL_WITHDRAWN, APPROVAL_CANCELLED}),
	APPROVAL_PENDING: frozenset({APPROVAL_APPROVED, APPROVAL_REJECTED, APPROVAL_CANCELLED}),
	APPROVAL_REJECTED: frozenset({APPROVAL_DRAFT}),
	APPROVAL_WITHDRAWN: frozenset({APPROVAL_DRAFT}),
	APPROVAL_APPROVED: frozenset(),
	APPROVAL_CANCELLED: frozenset(),
}


def is_terminal_approval_status(status: str) -> bool:
	"""True iff ``status`` allows no further approval transition (§3.2)."""
	return status in TERMINAL_APPROVAL_STATUSES


def is_valid_approval_transition(*, from_status: str | None, to_status: str) -> bool:
	"""True iff ``to_status`` is reachable from ``from_status`` (§3.2).

	``from_status=None`` models a brand-new approval request: only
	:data:`DEFAULT_APPROVAL_STATUS` (``Draft``) is a legal initial status, so a
	leader cannot forge ``Approved`` on create. Re-saving in the same status is
	always allowed (no-op transition).
	"""
	if to_status not in APPROVAL_STATUSES:
		return False
	if from_status is None:
		return to_status == DEFAULT_APPROVAL_STATUS
	if from_status == to_status:
		return True
	return to_status in APPROVAL_TRANSITIONS.get(from_status, frozenset())


def validate_approval_transition(*, from_status: str | None, to_status: str) -> None:
	"""Guard: raise :class:`FlockApprovalError` if the approval move is illegal."""
	if is_valid_approval_transition(from_status=from_status, to_status=to_status):
		return
	if from_status is None:
		raise FlockApprovalError(
			f"A new approval request must start as {DEFAULT_APPROVAL_STATUS!r} (got {to_status!r})."
		)
	raise FlockApprovalError(f"Illegal approval transition: {from_status!r} -> {to_status!r}.")


# ---------------------------------------------------------------------------- #
# Approval policy (FLO-7 §3.4). Chain rules resolved from a ``Flock Event
# Approval Policy`` row. Kept as plain frozen data so chain resolution stays
# pure — the controller loads the row and builds this struct.
# ---------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ApprovalPolicy:
	"""Resolved chain rules for one approval request (§3.4)."""

	require_branch_admin_final: bool = True
	max_approval_levels: int | None = None
	allow_self_approval: bool = False


#: The default policy when no ``Flock Event Approval Policy`` applies (§3.4 —
#: the nearest policy to the gathering's branch wins; absent one, the branch
#: admin is the terminal approver, self-approval is off, depth uncapped).
DEFAULT_POLICY = ApprovalPolicy()


# ---------------------------------------------------------------------------- #
# Chain-resolution gateway port (hexagonal) — the only tree/roster-touching
# surface :func:`resolve_approval_chain` needs. Production adapter:
# :class:`FrappeApprovalChainGateway` (lazy Frappe import, delegates to
# :class:`flock_os.traversal.TreeTraversalService`). Unit tests:
# :class:`RecordingApprovalChainGateway`. Returns plain data so the algorithm
# stays Frappe-agnostic and transport-agnostic.
# ---------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ChainNode:
	"""One group in the gathering→root path + its leadership roster (§3.3)."""

	group: str
	leaders: tuple[str, ...]
	"""Member ids leading ``group`` (accountable ``Flock Group.leader`` ∪ roster
	``Leader``/``Co-Leader`` edges, ADR §4.3), de-duplicated, nearest-first."""


@runtime_checkable
class ApprovalChainGateway(Protocol):
	"""Port: tree + roster reads chain resolution needs (§3.3 / §4.1)."""

	def chain_nodes_for_group(self, group: str) -> list[ChainNode]:
		"""Leadership roster per group on the ``group``→root path (own group first).

		Returns one :class:`ChainNode` per ancestor group, leaf→root, each
		carrying its leaders. This is the faithful tree-traversal input the
		algorithm dedupes + skips over (the traversal service's
		``leader_chain_for_group`` is the single-accountable-leader view; this
		port adds the roster co-leader axis).
		"""
		...

	def user_for_member(self, member: str) -> str | None:
		"""The ``User`` linked to ``member`` (``Flock Member.linked_user``), or ``None``."""
		...

	def group_branch(self, group: str) -> str | None:
		"""The ``Flock Branch`` of ``group`` — the approval's branch scope key."""
		...

	def branch_admin_member(self, branch: str) -> str | None:
		"""The ``Flock Member`` acting as Branch Admin for ``branch`` (terminal step)."""
		...

	def branch_admin_user(self, branch: str) -> str | None:
		"""The ``User`` of ``branch``'s Branch Admin, or ``None``."""
		...


class NullApprovalChainGateway:
	"""Empty gateway — the default before production wiring; yields no chain."""

	def chain_nodes_for_group(self, group: str) -> list[ChainNode]:  # noqa: ARG002
		return []

	def user_for_member(self, member: str) -> str | None:  # noqa: ARG002
		return None

	def group_branch(self, group: str) -> str | None:  # noqa: ARG002
		return None

	def branch_admin_member(self, branch: str) -> str | None:  # noqa: ARG002
		return None

	def branch_admin_user(self, branch: str) -> str | None:  # noqa: ARG002
		return None


# ---------------------------------------------------------------------------- #
# Resolved chain step (§3.3). One per approver; materialized into a
# ``Flock Event Approval Step`` row by the controller at submit time.
# ---------------------------------------------------------------------------- #
@dataclass(frozen=True)
class StepSpec:
	"""One resolved approver step (§3.3), ready to materialize as a step row."""

	idx: int
	approver_level: str
	approver_member: str | None
	approver_user: str | None
	approver_group: str | None
	step_status: str

	@property
	def is_self_skipped(self) -> bool:
		"""True iff this step was skipped because the leader is the requester (§3.3 #6)."""
		return self.step_status == STEP_SKIPPED


def _level_for_depth(depth: int) -> str:
	"""Map a chain node's depth to its ``approver_level`` label (§3.3).

	Depth 0 = the gathering's own group, depth 1 = its direct parent. Both are
	the "immediate" leaders → :data:`LEVEL_PARENT`. Deeper ancestors →
	:data:`LEVEL_ANCESTOR`. The Branch-Admin terminator is tagged separately by
	the caller (§3.3 #4).
	"""
	return LEVEL_PARENT if depth <= 1 else LEVEL_ANCESTOR


def resolve_approval_chain(
	*, group: str, requested_by: str, policy: ApprovalPolicy, gateway: ApprovalChainGateway
) -> list[StepSpec]:
	"""Resolve the approval chain walking ``parent_group`` up to branch-root (§4.1).

	Algorithm (FLO-7 §3.3 ``resolve_approval_chain``):

	Walks ``group``→root, collecting each group's leaders (accountable +
	roster). Each **unique** leader becomes a step, nearest-first (§3.3 #3/#7).
	Self-approval (leader == ``requested_by``) becomes ``Skipped`` unless
	``policy.allow_self_approval`` (§3.3 #6). If
	``policy.require_branch_admin_final``, the Branch Admin is appended as the
	terminal step (§3.3 #4), always kept even under the depth cap.
	``policy.max_approval_levels`` caps the leader (non-admin) depth, always
	preserving the Branch-Admin terminator (§3.3 #5).

	Returns the materialized :class:`StepSpec` list (idx 0..N), ``Skipped`` steps
	included for audit so the controller can show "auto-advanced past requester".

	A group with no leaders in the path is silently passed over (a leader-less
	ancestor contributes no step); an empty chain (no leaders + no branch admin)
	raises :class:`FlockApprovalError` — an approval with no possible approver is
	a configuration error, not a silent auto-approve.
	"""
	if not group:
		raise FlockApprovalError("group is required to resolve an approval chain.")
	if not requested_by:
		raise FlockApprovalError("requested_by is required to resolve an approval chain.")

	nodes = gateway.chain_nodes_for_group(group)
	specs: list[StepSpec] = []
	seen_leaders: set[str] = set()

	# Steps 1–3/#6/#7: leader walk.
	for depth, node in enumerate(nodes):
		level = _level_for_depth(depth)
		for leader in node.leaders:
			if not leader or leader in seen_leaders:
				continue
			seen_leaders.add(leader)
			is_self = leader == requested_by
			step_status = STEP_SKIPPED if (is_self and not policy.allow_self_approval) else STEP_PENDING
			specs.append(
				StepSpec(
					idx=len(specs),
					approver_level=level,
					approver_member=leader,
					approver_user=gateway.user_for_member(leader),
					approver_group=node.group,
					step_status=step_status,
				)
			)

	# Step 4: Branch-Admin terminator (always kept, §3.3 #4/#5).
	if policy.require_branch_admin_final:
		branch = gateway.group_branch(group)
		if branch:
			admin_member = gateway.branch_admin_member(branch)
			if admin_member:
				specs.append(
					StepSpec(
						idx=len(specs),
						approver_level=LEVEL_BRANCH_ADMIN,
						approver_member=admin_member,
						approver_user=gateway.branch_admin_user(branch),
						approver_group=None,
						step_status=STEP_PENDING,
					)
				)

	# Step 5: depth cap on the leader steps — terminator preserved (§3.3 #5).
	# ``Skipped`` self-steps are audit-only, so they don't consume budget: the
	# cap counts real (``Pending``) approval levels, keeping the audit trail of
	# the self-skip intact. ``require_branch_admin_final`` is always honored.
	if policy.max_approval_levels is not None and policy.max_approval_levels >= 0:
		leader_steps = [s for s in specs if s.approver_level != LEVEL_BRANCH_ADMIN]
		admin_steps = [s for s in specs if s.approver_level == LEVEL_BRANCH_ADMIN]
		kept: list[StepSpec] = []
		budget = policy.max_approval_levels
		for step in leader_steps:
			if step.step_status == STEP_SKIPPED:
				kept.append(step)  # audit-only, never counts toward the cap
			elif budget > 0:
				kept.append(step)
				budget -= 1
			# else: drop the step — depth cap reached
		specs = [_reindex(s, i) for i, s in enumerate([*kept, *admin_steps])]

	# A chain with no approver at all is a misconfiguration — never a silent
	# auto-approve (a one-time event with zero approvers must surface, not pass).
	if not any(s.step_status == STEP_PENDING for s in specs):
		raise FlockApprovalError(
			f"Approval chain for group {group!r} resolved no approver — "
			"set a leader on an ancestor group or a Branch Admin for its branch."
		)
	return specs


def _reindex(step: StepSpec, idx: int) -> StepSpec:
	"""Return a copy of ``step`` with its ``idx`` reset (after a depth-cap reorder)."""
	return replace(step, idx=idx)


# ---------------------------------------------------------------------------- #
# Step advancement (§4.2). Pure helpers the controller uses to advance
# ``current_step`` after a decision and to detect final approval.
# ---------------------------------------------------------------------------- #
def first_pending_step(steps: Sequence[StepSpec | StepView]) -> int | None:
	"""Index of the first ``Pending`` step, or ``None`` if the chain is complete.

	``Skipped`` steps are not pending, so a freshly submitted chain auto-advances
	past self-approval skips to the first real approver (§3.3 #6).
	"""
	for i, step in enumerate(steps):
		if _step_status(step) == STEP_PENDING:
			return i
	return None


def is_chain_complete(steps: Sequence[StepSpec | StepView]) -> bool:
	"""True iff no step remains ``Pending`` (the chain reached final approval, §4.2)."""
	return first_pending_step(steps) is None


def _step_status(step: StepSpec | StepView) -> str:
	"""Read ``step_status`` off either a :class:`StepSpec` or a :class:`StepView`."""
	return step.step_status


def current_step_index(steps: Sequence[StepSpec | StepView], stored_current: int = 0) -> int | None:
	"""The step actually awaiting action: the first ``Pending`` step at/after ``stored_current``.

	On submit (and after each approval) the controller passes the stored
	``current_step``; this jumps it past any ``Skipped`` steps so the next actor
	is the true next approver. Returns ``None`` once the chain is complete.
	"""
	for i in range(stored_current, len(steps)):
		if _step_status(steps[i]) == STEP_PENDING:
			return i
	return None


# ---------------------------------------------------------------------------- #
# StepView — a transport-neutral view of one materialized step the scope guard
# (``flock_os.permissions.assert_approval_scope``) consumes. Kept structurally
# typed (no Frappe import) so the guard is unit-testable with plain dicts/views
# and so :mod:`flock_os.permissions` need not depend on this module at import
# time (avoids an import cycle: the REST actions here import the guard lazily).
# ---------------------------------------------------------------------------- #
@dataclass(frozen=True)
class StepView:
	"""A materialized step's guard-relevant fields (§4.2 / §6.2)."""

	idx: int
	approver_level: str
	approver_member: str | None
	approver_user: str | None
	approver_group: str | None
	step_status: str
	doc_branch: str | None
	doc_group: str | None


def step_view_from_spec(spec: StepSpec, *, doc_branch: str | None, doc_group: str | None) -> StepView:
	"""Lift a freshly resolved :class:`StepSpec` into a :class:`StepView` for the guard."""
	return StepView(
		idx=spec.idx,
		approver_level=spec.approver_level,
		approver_member=spec.approver_member,
		approver_user=spec.approver_user,
		approver_group=spec.approver_group,
		step_status=spec.step_status,
		doc_branch=doc_branch,
		doc_group=doc_group,
	)


__all__ = (
	"APPROVAL_STATUSES",
	"APPROVAL_TRANSITIONS",
	"APPROVAL_DRAFT",
	"APPROVAL_PENDING",
	"APPROVAL_APPROVED",
	"APPROVAL_REJECTED",
	"APPROVAL_CANCELLED",
	"APPROVAL_WITHDRAWN",
	"APPROVER_LEVELS",
	"DEFAULT_APPROVAL_STATUS",
	"DEFAULT_POLICY",
	"LEVEL_ANCESTOR",
	"LEVEL_BRANCH_ADMIN",
	"LEVEL_PARENT",
	"STEP_APPROVED",
	"STEP_DECIDED",
	"STEP_PENDING",
	"STEP_REJECTED",
	"STEP_RECUSED",
	"STEP_SKIPPED",
	"STEP_STATUSES",
	"TERMINAL_APPROVAL_STATUSES",
	"ApprovalChainGateway",
	"ApprovalPolicy",
	"ChainNode",
	"FlockApprovalError",
	"NullApprovalChainGateway",
	"StepSpec",
	"StepView",
	"current_step_index",
	"first_pending_step",
	"is_chain_complete",
	"is_terminal_approval_status",
	"is_valid_approval_transition",
	"resolve_approval_chain",
	"step_view_from_spec",
	"validate_approval_transition",
)
