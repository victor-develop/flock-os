"""
Domain logic for scoped one-time-event registration — the pure invariants
(FLO-7 §3.5 / §5, materialized by [FLO-62]).

This is the **pure** half of scoped registration: the scope + window + capacity
predicates the DocType controller enforces. Kept framework-agnostic (no Frappe
import) so it runs under plain ``pytest`` — the same hexagonal discipline as
:mod:`flock_os.approvals` / :mod:`flock_os.gatherings` / :mod:`flock_os.permissions`.

It owns four things:

* **Registration status catalog + state machine** (§3.5) — a registration row's
  lifecycle: ``Registered → {Checked-in | Cancelled}`` and the capacity-driven
  ``Waitlisted → Registered`` promotion (auto-promotion itself is the Phase B
  bulk path, [FLO-79]; the legal *transitions* are owned here so the gate is
  single-source).
* **Registration scope catalog** (§5) — the eligible-set enum the final
  approver confirms (``Own Group`` / ``Group Subtree`` / ``Branch`` /
  ``Branch Subtree`` / ``Org-wide`` / ``Invited Only``).
* **Eligibility predicate** (:func:`is_member_in_scope`) — pure test that a
  member falls inside a gathering's confirmed scope, over a hexagonal
  :class:`RegistrationScopeGateway` port (membership / branch / subtree reads).
* **Window + capacity decision** (:func:`is_registration_window_open` /
  :func:`capacity_decision`) — the open/close + capacity rules that gate every
  ``register_for_event`` call, expressed without I/O so the 15k-scale atomic
  conditional ``UPDATE`` (§5 #3) is driven by a unit-testable rule.

The Frappe-coupled half lives elsewhere:

* the DocType controller (``flock_event_registration.py``) owns the doc
  lifecycle, the scope reads (via :class:`FrappeRegistrationScopeGateway`
  delegating to :class:`flock_os.traversal.TreeTraversalService`), the atomic
  capacity ``UPDATE``, and the ``@frappe.whitelist()`` REST actions
  (FLO-7 §8) — Frappe's natural home, coverage-omitted;
* :mod:`flock_os.events` is the canonical event publisher (§7) — registration
  state changes emit there, never scattered in the DocType.

Layering (ADR-0001 §2 separation of concerns)::

    @frappe.whitelist() actions            (doctype controller, transport)
      -> is_member_in_scope(...)            (THIS module, pure domain)
      |   |-> RegistrationScopeGateway port (membership/branch reads, hexagonal)
      -> is_registration_window_open(...)   (THIS module, pure domain)
      -> capacity_decision(...)             (THIS module, pure domain)
      -> flock_os.events.emit(...)          (canonical event publisher)
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

# ---------------------------------------------------------------------------- #
# Registration scope catalog (FLO-7 §5). The confirmed ``registration_scope``
# ``Select`` options. Kept here (not in fixtures) because :func:`is_member_in_scope`
# keys on the exact strings — the catalog must stay a single source of truth,
# exactly like :mod:`flock_os.approvals`.
# ---------------------------------------------------------------------------- #
SCOPE_NONE = "None"
SCOPE_OWN_GROUP = "Own Group"
SCOPE_GROUP_SUBTREE = "Group Subtree"
SCOPE_BRANCH = "Branch"
SCOPE_BRANCH_SUBTREE = "Branch Subtree"
SCOPE_ORG_WIDE = "Org-wide"
SCOPE_INVITED_ONLY = "Invited Only"

#: The ordered ``Select`` options for ``Flock Gathering.registration_scope`` (§5).
#: Mirrors ``Flock Event Approval.proposed_registration_scope`` so the approval
#: confirm copy is a plain assignment (§4.2 #2).
REGISTRATION_SCOPES: tuple[str, ...] = (
	SCOPE_NONE,
	SCOPE_OWN_GROUP,
	SCOPE_GROUP_SUBTREE,
	SCOPE_BRANCH,
	SCOPE_BRANCH_SUBTREE,
	SCOPE_ORG_WIDE,
	SCOPE_INVITED_ONLY,
)

#: The default scope when an approver has not overridden the leader's proposal
#: (§3.4 ``default_registration_scope``). ``Own Group`` is the tightest non-empty
#: scope — the safe floor; broader scopes require an explicit confirm.
DEFAULT_REGISTRATION_SCOPE = SCOPE_OWN_GROUP

#: Scopes that close registration entirely (no eligible set). ``None`` (the
#: field default before approval) and ``None`` literal both mean "not open".
CLOSED_SCOPES: frozenset[str] = frozenset({SCOPE_NONE})


class FlockRegistrationError(ValueError):
	"""Raised when a registration invariant (scope / window / capacity) is violated."""


# ---------------------------------------------------------------------------- #
# Registration status catalog + state machine (FLO-7 §3.5).
# ---------------------------------------------------------------------------- #
REGISTRATION_REGISTERED = "Registered"
REGISTRATION_WAITLISTED = "Waitlisted"
REGISTRATION_CANCELLED = "Cancelled"
REGISTRATION_CHECKED_IN = "Checked-in"
REGISTRATION_NO_SHOW = "No-show"

#: The ordered ``Select`` options for ``Flock Event Registration.registration_status`` (§3.5).
REGISTRATION_STATUSES: tuple[str, ...] = (
	REGISTRATION_REGISTERED,
	REGISTRATION_WAITLISTED,
	REGISTRATION_CANCELLED,
	REGISTRATION_CHECKED_IN,
	REGISTRATION_NO_SHOW,
)

#: Default status for a newly created registration (§3.5).
DEFAULT_REGISTRATION_STATUS = REGISTRATION_REGISTERED

#: Registration statuses that no longer count toward ``registered_count`` (the
#: atomic capacity counter). ``Waitlisted`` is tracked separately; ``Cancelled``
#: frees a seat (Phase B auto-promotes the oldest waitlister, [FLO-79]).
INACTIVE_REGISTRATION_STATUSES: frozenset[str] = frozenset({REGISTRATION_CANCELLED, REGISTRATION_NO_SHOW})

#: The legal ``from -> {to, ...}`` registration transitions (§3.5 lifecycle).
#: ``Registered → Checked-in`` (the FLO-6 attendance bridge), ``Registered →
#: Cancelled`` (registrant/leader/admin), ``Waitlisted → Registered`` (capacity
#: frees — the Phase B auto-promotion path), ``Waitlisted → Cancelled``.
#: ``Checked-in`` and ``No-show`` are terminal post-event states.
REGISTRATION_TRANSITIONS: dict[str, frozenset[str]] = {
	REGISTRATION_REGISTERED: frozenset({REGISTRATION_CHECKED_IN, REGISTRATION_CANCELLED}),
	REGISTRATION_WAITLISTED: frozenset({REGISTRATION_REGISTERED, REGISTRATION_CANCELLED}),
	REGISTRATION_CANCELLED: frozenset(),
	REGISTRATION_CHECKED_IN: frozenset(),
	REGISTRATION_NO_SHOW: frozenset(),
}

#: Statuses from which no further transition is allowed (§3.5 terminal).
TERMINAL_REGISTRATION_STATUSES: frozenset[str] = frozenset(
	{REGISTRATION_CANCELLED, REGISTRATION_CHECKED_IN, REGISTRATION_NO_SHOW}
)

#: How a registration was created (§3.5 ``registered_via``). ``Bulk`` +
#: ``Invite`` are exercised by the Phase B paths ([FLO-79]); the MVP path is
#: ``Self`` / ``Leader``.
VIA_SELF = "Self"
VIA_LEADER = "Leader"
VIA_INVITE = "Invite"
VIA_BULK = "Bulk"
REGISTRATION_VIA: tuple[str, ...] = (VIA_SELF, VIA_LEADER, VIA_INVITE, VIA_BULK)
DEFAULT_REGISTRATION_VIA = VIA_SELF


def is_terminal_registration_status(status: str) -> bool:
	"""True iff ``status`` allows no further registration transition (§3.5)."""
	return status in TERMINAL_REGISTRATION_STATUSES


def is_valid_registration_transition(*, from_status: str, to_status: str) -> bool:
	"""True iff ``to_status`` is reachable from ``from_status`` (§3.5).

	Re-applying the same status is always allowed (no-op transition) so the
	controller's save-after-decide path is idempotent. ``Registered → Registered``
	guards a duplicate check-in attempt, for instance.
	"""
	if to_status not in REGISTRATION_STATUSES:
		return False
	if from_status == to_status:
		return True
	return to_status in REGISTRATION_TRANSITIONS.get(from_status, frozenset())


def validate_registration_transition(*, from_status: str, to_status: str) -> None:
	"""Guard: raise :class:`FlockRegistrationError` if the registration move is illegal."""
	if is_valid_registration_transition(from_status=from_status, to_status=to_status):
		return
	raise FlockRegistrationError(
		f"Illegal registration transition: {from_status!r} -> {to_status!r} (FLO-7 §3.5)."
	)


# ---------------------------------------------------------------------------- #
# Scope-resolution gateway port (hexagonal) — the only tree/roster-touching
# surface :func:`is_member_in_scope` needs. Production adapter:
# :class:`FrappeRegistrationScopeGateway` (lazy Frappe import, delegates to
# :class:`flock_os.traversal.TreeTraversalService`). Unit tests:
# :class:`RecordingRegistrationScopeGateway`. Returns plain data so the
# eligibility predicate stays Frappe-agnostic + transport-agnostic.
# ---------------------------------------------------------------------------- #
@dataclass(frozen=True)
class GatheringScope:
	"""A gathering's scope anchors (branch/group/organization) read in one query.

	The PERF-REG-N1 collapse ([FLO-518]): the three gathering reads the
	registration paths need (branch + group + organization) are fetched in a
	single ``db.get_value(..., [branch, group, organization], as_dict=True)``
	rather than three serial single-column reads. Carried as a value object so
	:class:`RegistrationScopeGateway.gathering_scope` stays transport-agnostic
	and the eligibility predicate / row-build read it once, not three times.
	"""

	branch: str | None
	group: str | None
	organization: str | None


@runtime_checkable
class RegistrationScopeGateway(Protocol):
	"""Port: the membership / branch / subtree reads eligibility needs (§5).

	Phase B ([FLO-79]) adds :meth:`has_valid_invitation` so the ``Invited Only``
	scope (§5) can be honored without a Frappe import here — the production
	adapter delegates to the ``Flock Event Invitation`` table.
	"""

	def member_branch(self, member: str) -> str | None:
		"""The ``Flock Branch`` the member belongs to (their home branch)."""
		...

	def member_organization(self, member: str) -> str | None:
		"""The ``Flock Organization`` of the member's branch (tenant floor)."""
		...

	def member_groups(self, member: str) -> tuple[str, ...]:
		"""Groups the member is an active roster member of (§5 ``Own Group`` set)."""
		...

	def group_subtree(self, group: str) -> tuple[str, ...]:
		"""``group`` + its descendant groups (§5 ``Group Subtree`` eligible set)."""
		...

	def branch_subtree(self, branch: str) -> tuple[str, ...]:
		"""``branch`` + its descendant branches (§5 ``Branch Subtree`` set)."""
		...

	def gathering_branch(self, gathering: str) -> str | None:
		"""The gathering's branch scope key."""
		...

	def gathering_organization(self, gathering: str) -> str | None:
		"""The gathering's organization (tenant floor)."""
		...

	def gathering_group(self, gathering: str) -> str | None:
		"""The gathering's originating group (the ``Own Group`` anchor)."""
		...

	def gathering_scope(self, gathering: str) -> GatheringScope:
		"""The gathering's branch + group + organization in one read (PERF-REG-N1, [FLO-518]).

		Collapses the three serial single-column reads
		(:meth:`gathering_branch` / :meth:`gathering_group` /
		:meth:`gathering_organization`) into a single query. Call sites that
		need all three anchors (the eligibility predicate, the registration
		row-build) use this; the individual accessors stay for single-field reads.
		"""
		...

	def has_valid_invitation(self, *, gathering: str, member: str) -> bool:
		"""True iff ``member`` holds a non-expired, non-declined invitation (§5).

		``Invited Only`` eligibility: a ``Flock Event Invitation`` row exists for
		(gathering, member) whose status is ``Sent`` or ``Accepted`` and whose
		``expires_on`` has not lapsed. Declined/Expired invitations are out of
		scope. Group-subtree invitations (``invitee_group``) qualify a member
		who belongs to that subtree. Phase B ([FLO-79]).
		"""
		...


class NullRegistrationScopeGateway:
	"""Empty gateway — the default before production wiring; nothing is in scope."""

	def member_branch(self, member: str) -> str | None:  # noqa: ARG002
		return None

	def member_organization(self, member: str) -> str | None:  # noqa: ARG002
		return None

	def member_groups(self, member: str) -> tuple[str, ...]:  # noqa: ARG002
		return ()

	def group_subtree(self, group: str) -> tuple[str, ...]:  # noqa: ARG002
		return ()

	def branch_subtree(self, branch: str) -> tuple[str, ...]:  # noqa: ARG002
		return ()

	def gathering_branch(self, gathering: str) -> str | None:  # noqa: ARG002
		return None

	def gathering_organization(self, gathering: str) -> str | None:  # noqa: ARG002
		return None

	def gathering_group(self, gathering: str) -> str | None:  # noqa: ARG002
		return None

	def gathering_scope(self, gathering: str) -> GatheringScope:  # noqa: ARG002
		return GatheringScope(branch=None, group=None, organization=None)

	def has_valid_invitation(self, *, gathering: str, member: str) -> bool:  # noqa: ARG002
		return False


# ---------------------------------------------------------------------------- #
# Invitation expiry (§3.6 / §5 Invited Only). Pure so the controller + the
# eligibility gate share one verdict; ``now`` is passed in to keep this testable
# without a clock. A null ``expires_on`` means the invitation never lapses.
# ---------------------------------------------------------------------------- #
def is_invitation_expired(expires_on: str | None, *, now: str) -> bool:
	"""True iff ``expires_on`` is set and has passed (``now`` >= ``expires_on``).

	Pure: the controller passes ``frappe.utils.now()``. A null/empty
	``expires_on`` means the invitation does not expire (§3.6).
	"""
	if not expires_on:
		return False
	return now >= expires_on


# ---------------------------------------------------------------------------- #
# Eligibility predicate (§5). Pure over the gateway port so the scope rules are
# unit-testable without a bench.
# ---------------------------------------------------------------------------- #
def is_member_in_scope(*, member: str, gathering: str, scope: str, gateway: RegistrationScopeGateway) -> bool:
	"""True iff ``member`` falls inside ``gathering``'s confirmed ``scope`` (§5).

	Eligible set per scope (the server-side gate every ``register_for_event``
	runs before any row is created — out-of-scope registration is rejected):

	* ``Own Group`` — active roster member of the gathering's group.
	* ``Group Subtree`` — member of the gathering's group + any descendant group.
	* ``Branch`` — the member's home branch equals the gathering's branch.
	* ``Branch Subtree`` — the member's branch is in the gathering's branch subtree.
	* ``Org-wide`` — the member shares the gathering's organization.
	* ``Invited Only`` — only a valid, non-expired ``Flock Event
	Invitation`` holder qualifies (Phase B, [FLO-79]); until invitations
	land the predicate returns ``False`` (fail closed).
	* ``None`` — registration closed (no eligible set).
	"""
	if not member or not gathering:
		return False
	if scope in CLOSED_SCOPES:
		return False

	# PERF-REG-N1 ([FLO-518]): one gathering read, not three.
	gathering_anchors = gateway.gathering_scope(gathering)
	gathering_group = gathering_anchors.group
	gathering_branch = gathering_anchors.branch
	gathering_org = gathering_anchors.organization

	if scope == SCOPE_OWN_GROUP:
		if not gathering_group:
			return False
		return gathering_group in set(gateway.member_groups(member))
	if scope == SCOPE_GROUP_SUBTREE:
		if not gathering_group:
			return False
		subtree = set(gateway.group_subtree(gathering_group))
		if not subtree:
			return False
		return bool(set(gateway.member_groups(member)) & subtree)
	if scope == SCOPE_BRANCH:
		if not gathering_branch:
			return False
		return gateway.member_branch(member) == gathering_branch
	if scope == SCOPE_BRANCH_SUBTREE:
		if not gathering_branch:
			return False
		subtree = set(gateway.branch_subtree(gathering_branch))
		if not subtree:
			return False
		return gateway.member_branch(member) in subtree
	if scope == SCOPE_ORG_WIDE:
		if not gathering_org:
			return False
		return gateway.member_organization(member) == gathering_org
	if scope == SCOPE_INVITED_ONLY:
		# Phase B ([FLO-79]): the eligible set is the holders of a valid,
		# non-expired ``Flock Event Invitation``. The gateway's
		# ``has_valid_invitation`` honors Sent/Accepted, non-expired rows
		# (group-subtree invitations qualify a member in that subtree). Fail
		# closed if the gateway cannot resolve an invitation.
		return gateway.has_valid_invitation(gathering=gathering, member=member)
	# Unknown scope string — fail closed (never silently admit).
	raise FlockRegistrationError(f"Unknown registration scope {scope!r} (FLO-7 §5).")


def eligibility_reason(*, member: str, gathering: str, scope: str, gateway: RegistrationScopeGateway) -> str:
	"""A human-readable in/out reason for ``get_registration_eligibility`` (§8).

	Pure: delegates to :func:`is_member_in_scope` and renders the verdict +
	the matched scope rule. The controller returns this verbatim as the UI hint
	(no side effects — the REST surface is read-only).
	"""
	if scope in CLOSED_SCOPES:
		return f"Registration is closed for this event (scope={scope!r})."
	if scope == SCOPE_INVITED_ONLY:
		# Phase B ([FLO-79]): the verdict now rides the gateway, so the reason
		# reflects the real invitation state.
		if is_member_in_scope(member=member, gathering=gathering, scope=scope, gateway=gateway):
			return f"Member is eligible (matches scope {scope!r})."
		return f"Member is out of scope ({scope!r}) — no valid invitation for this event."
	if is_member_in_scope(member=member, gathering=gathering, scope=scope, gateway=gateway):
		return f"Member is eligible (matches scope {scope!r})."
	return f"Member is out of scope ({scope!r}) for this event."


# ---------------------------------------------------------------------------- #
# Window + capacity decision (§5). Pure helpers — no I/O — so the 15k-scale
# atomic conditional UPDATE is driven by a unit-testable rule.
# ---------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RegistrationWindow:
	"""The open/close gate inputs resolved from the gathering (§5 #1)."""

	approval_status: str
	scope: str
	opens_on: str | None
	closes_on: str | None
	capacity: int | None
	registered_count: int


def is_registration_window_open(window: RegistrationWindow, *, now: str) -> bool:
	"""True iff the gathering is approved + in-window + scope is not closed (§5 #1).

	``now`` is an ISO datetime string the controller supplies
	(``frappe.utils.now()``); passing it in keeps this pure. A null window bound
	(``opens_on``/``closes_on``) means unbounded on that side — the spec lets a
	leader leave the window open-ended.
	"""
	if window.scope in CLOSED_SCOPES:
		return False
	# Registration is gated on final approval (§4.2 #2): the denormalized
	# ``approval_status`` on the gathering must read ``Approved``. Pending /
	# Rejected / Draft events are not open for registration.
	if window.approval_status != "Approved":
		return False
	if window.opens_on and now < window.opens_on:
		return False
	if window.closes_on and now > window.closes_on:
		return False
	return True


@dataclass(frozen=True)
class CapacityDecision:
	"""Outcome of a capacity check (§5 #3): the status to record + whether seated."""

	status: str
	seated: bool

	@property
	def is_waitlisted(self) -> bool:
		"""True iff this decision placed the registrant on the waitlist."""
		return self.status == REGISTRATION_WAITLISTED


def capacity_decision(*, capacity: int | None, registered_count: int) -> CapacityDecision:
	"""Decide ``Registered`` vs ``Waitlisted`` against the capacity floor (§5 #3).

	Pure: the controller reads ``registered_count`` + ``capacity`` then runs this
	to learn the intended status *before* the atomic conditional ``UPDATE``
	(§5 #3). A null/zero capacity means uncapped (always seated). The atomic
	UPDATE is the race-correctness backstop at 15k concurrency — this helper
	only computes the optimistic intent so the controller can emit the right
	event (``created`` vs ``waitlisted``) on the affected-rows verdict.

	The policy's ``enable_waitlist`` toggle (§3.4, Phase B/[FLO-79]) is honored
	at the controller: when the waitlist is disabled and the event is full, the
	controller rejects rather than queuing — this helper stays the single seat/
	no-seat verdict that both the waitlist-on and waitlist-off paths branch on.
	"""
	if capacity is None or capacity <= 0:
		return CapacityDecision(status=REGISTRATION_REGISTERED, seated=True)
	if registered_count < capacity:
		return CapacityDecision(status=REGISTRATION_REGISTERED, seated=True)
	return CapacityDecision(status=REGISTRATION_WAITLISTED, seated=False)


def is_capacity_full(*, capacity: int | None, registered_count: int) -> bool:
	"""True iff the event is at/over capacity (no seat remains) (§5 #3).

	Convenience for the eligibility hint (``get_registration_eligibility``).
	Uncapped events (null/zero capacity) are never full.
	"""
	if capacity is None or capacity <= 0:
		return False
	return registered_count >= capacity


def is_gathering_registration_eligible(
	*, window: RegistrationWindow, now: str, member: str, gathering: str, gateway: RegistrationScopeGateway
) -> bool:
	"""Composite open + scope gate (§5 #1–#2): window open AND member in scope.

	The controller's pre-write guard. Returns ``True`` only when the window is
	open (approved + in-window + not-closed scope) AND the member falls inside
	the confirmed scope. Out-of-scope or closed-window registration is rejected
	before any row is created.
	"""
	return is_registration_window_open(window, now=now) and is_member_in_scope(
		member=member, gathering=gathering, scope=window.scope, gateway=gateway
	)


# ---------------------------------------------------------------------------- #
# Waitlist auto-promotion (FLO-7 §5 #6, Phase B/[FLO-79]).
#
# On a ``Registered → Cancelled`` transition a capacity seat frees; the oldest
# ``Waitlisted`` row is atomically promoted to ``Registered``. The selection
# rule (oldest-first FIFO) is pure so the gate can test it without a DB; the
# atomic promotion ``UPDATE`` (race-free at 15k) lives in the controller.
# ---------------------------------------------------------------------------- #
@dataclass(frozen=True)
class WaitlistCandidate:
	"""A waitlisted registration's promotion-relevant fields (§5 #6)."""

	name: str
	gathering: str
	registrant: str
	registered_at: str


def select_waitlist_promotion_candidate(
	candidates: Sequence[WaitlistCandidate],
) -> WaitlistCandidate | None:
	"""Pick the oldest ``Waitlisted`` candidate to promote (§5 #6, FIFO).

	Pure over an already-ordered-or-not candidate list: the oldest by
	``registered_at`` (earliest waitlist timestamp wins — first-come-first-
	served). Ties break on ``name`` for determinism (stable across replays so
	the at-least-once outbox replay ([FLO-4](/FLO/issues/FLO-4) §5.1) promotes
	the same row). Returns ``None`` when the waitlist is empty (no promotion).

	The controller supplies the candidate rows (read from the registration
	table); this helper owns only the ordering rule so it is unit-testable.
	"""
	if not candidates:
		return None
	return min(candidates, key=lambda c: (c.registered_at, c.name))


# ---------------------------------------------------------------------------- #
# Bulk registration batching (FLO-7 §5, Phase B/[FLO-79]).
#
# ``register_bulk`` validates scope once then enqueues per-batch inserts on the
# Frappe/RQ queue — the 15k path. The chunk shape (batch size) is pure so the
# gate can test the partitioning without a worker; the enqueue + the per-batch
# insert live in the controller.
# ---------------------------------------------------------------------------- #
#: Default per-batch size for bulk registration (mirrors the FLO-6 §5 attendance
#: bulk path; small enough to keep each transaction's lock window tight at 15k
#: burst, large enough to avoid 15k round-trips). The controller may override.
DEFAULT_BULK_BATCH_SIZE = 500


def chunk_members(members: Sequence[str], *, batch_size: int = DEFAULT_BULK_BATCH_SIZE) -> list[list[str]]:
	"""Partition ``members`` into ordered batches for queue-backed insert (§5).

	Pure: the bulk path's only pure concern is the partition shape. Validates
	``batch_size`` (>=1) and preserves input order within + across batches so
	the per-batch jobs are deterministic (idempotent on replay via the unique
	``(gathering, registrant)`` constraint). De-duplicates members first so a
	15k list with repeats never double-inserts (the unique index is the
	backstop, but de-dup here keeps the batch counts honest).
	"""
	if batch_size < 1:
		raise FlockRegistrationError(f"batch_size must be >= 1 (got {batch_size}).")
	# De-duplicate preserving order (the unique constraint backstops a race, but
	# a pre-pass keeps the reported totals accurate + the batches tight).
	seen: dict[str, None] = dict.fromkeys(m for m in members if m)
	unique = list(seen)
	return [unique[i : i + batch_size] for i in range(0, len(unique), batch_size)]


__all__ = (
	"CLOSED_SCOPES",
	"DEFAULT_REGISTRATION_SCOPE",
	"DEFAULT_REGISTRATION_STATUS",
	"DEFAULT_REGISTRATION_VIA",
	"INACTIVE_REGISTRATION_STATUSES",
	"REGISTRATION_CANCELLED",
	"REGISTRATION_CHECKED_IN",
	"REGISTRATION_NO_SHOW",
	"REGISTRATION_REGISTERED",
	"REGISTRATION_SCOPES",
	"REGISTRATION_STATUSES",
	"REGISTRATION_TRANSITIONS",
	"REGISTRATION_VIA",
	"REGISTRATION_WAITLISTED",
	"SCOPE_BRANCH",
	"SCOPE_BRANCH_SUBTREE",
	"SCOPE_GROUP_SUBTREE",
	"SCOPE_INVITED_ONLY",
	"SCOPE_NONE",
	"SCOPE_ORG_WIDE",
	"SCOPE_OWN_GROUP",
	"TERMINAL_REGISTRATION_STATUSES",
	"VIA_BULK",
	"VIA_INVITE",
	"VIA_LEADER",
	"VIA_SELF",
	"CapacityDecision",
	"DEFAULT_BULK_BATCH_SIZE",
	"FlockRegistrationError",
	"GatheringScope",
	"NullRegistrationScopeGateway",
	"RegistrationScopeGateway",
	"RegistrationWindow",
	"WaitlistCandidate",
	"capacity_decision",
	"chunk_members",
	"eligibility_reason",
	"is_capacity_full",
	"is_gathering_registration_eligible",
	"is_invitation_expired",
	"is_member_in_scope",
	"is_registration_window_open",
	"is_terminal_registration_status",
	"is_valid_registration_transition",
	"select_waitlist_promotion_candidate",
	"validate_registration_transition",
)
