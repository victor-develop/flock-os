"""
Domain logic for ``Flock Gathering`` — the canonical event/gathering entity
(FLO-6 §3.2/§4, ADR-0001 §3).

``Flock Gathering`` is the **single source of truth for "a meeting happened /
will happen"**: both routine gatherings ([FLO-6](/FLO/issues/FLO-6)) and
one-time events ([FLO-7](/FLO/issues/FLO-7)) are ``Flock Gathering`` rows. This
module owns the *pure* invariants the DocType controller enforces — kept
framework-agnostic (no Frappe import) so they run under plain ``pytest``, the
same hexagonal discipline as :mod:`flock_os.flock_os.trees` /
:mod:`flock_os.permissions`:

* **Reporting state machine** (§4) — ``Scheduled → Held → Reported →
  Confirmed`` with ``Cancelled`` terminal from Scheduled/Held and a
  ``Reported → Held`` reject/reopen path. The controller guards every save;
  the REST actions that *drive* transitions + emit the report events land in
  the leader reporting workflow ([FLO-56](/FLO/issues/FLO-56)).
* **Branch-binding** — a gathering is branch-bound to its group (the group
  subtree is branch-bound per ADR §4.2, so the gathering's ``branch`` must
  equal its group's ``branch``). Mirrors :func:`trees.validate_group_branch_binding`.
* **group_path denormalization** — the roll-up helper (ADR §9: reporting
  denorm only, **no** permission semantics). Built root-first slash-delimited
  so a subtree roll-up can ``LIKE '/<root>/%'``.

The Frappe-coupled half (DB lookups for the group's branch / parent chain, the
``validate`` hook) lives in the DocType controller
(:mod:`flock_os.flock_os.doctype.flock_gathering.flock_gathering`).
"""

from __future__ import annotations

from collections.abc import Sequence

# ---------------------------------------------------------------------------- #
# Reporting status catalog (FLO-6 §4). The canonical ``Select`` options for
# ``Flock Gathering.status``. Kept here (not in fixtures) because the state
# machine below keys on the exact strings — the catalog + the transition table
# must stay a single source of truth.
# ---------------------------------------------------------------------------- #
STATUS_SCHEDULED = "Scheduled"
STATUS_HELD = "Held"
STATUS_REPORTED = "Reported"
STATUS_CONFIRMED = "Confirmed"
STATUS_CANCELLED = "Cancelled"

#: The ordered ``Select`` options for ``Flock Gathering.status`` (FLO-6 §4).
GATHERING_STATUSES: tuple[str, ...] = (
	STATUS_SCHEDULED,
	STATUS_HELD,
	STATUS_REPORTED,
	STATUS_CONFIRMED,
	STATUS_CANCELLED,
)

#: Default status for a newly created gathering (FLO-6 §4 — ``Scheduled``).
DEFAULT_STATUS = STATUS_SCHEDULED

#: Statuses from which no further transition is allowed (FLO-6 §4).
TERMINAL_STATUSES: frozenset[str] = frozenset({STATUS_CONFIRMED, STATUS_CANCELLED})


class FlockGatheringError(ValueError):
	"""Raised when a gathering invariant (state machine / binding) is violated."""


# ---------------------------------------------------------------------------- #
# Reporting state machine (FLO-6 §4).
# ---------------------------------------------------------------------------- #
#: Legal ``from -> {to, ...}`` transitions (FLO-6 §4 diagram).
TRANSITIONS: dict[str, frozenset[str]] = {
	STATUS_SCHEDULED: frozenset({STATUS_HELD, STATUS_CANCELLED}),
	STATUS_HELD: frozenset({STATUS_REPORTED, STATUS_CANCELLED}),
	STATUS_REPORTED: frozenset({STATUS_CONFIRMED, STATUS_HELD}),
	STATUS_CONFIRMED: frozenset(),
	STATUS_CANCELLED: frozenset(),
}


def is_terminal_status(status: str) -> bool:
	"""True iff ``status`` allows no further transition (Confirmed / Cancelled)."""
	return status in TERMINAL_STATUSES


def is_valid_transition(*, from_status: str | None, to_status: str) -> bool:
	"""True iff ``to_status`` is reachable from ``from_status`` (FLO-6 §4).

	``from_status=None`` models a brand-new gathering: only :data:`DEFAULT_STATUS`
	(``Scheduled``) is a legal initial status. Every other status must arrive via
	a transition. This keeps a leader from forging ``Confirmed`` on create.
	"""
	if to_status not in GATHERING_STATUSES:
		return False
	if from_status is None:
		return to_status == DEFAULT_STATUS
	if from_status == to_status:
		# Re-saving in the same status is always allowed (no-op transition).
		return True
	return to_status in TRANSITIONS.get(from_status, frozenset())


def validate_status_transition(*, from_status: str | None, to_status: str) -> None:
	"""Guard: raise :class:`FlockGatheringError` if the move is illegal (§4)."""
	if is_valid_transition(from_status=from_status, to_status=to_status):
		return
	if from_status is None:
		raise FlockGatheringError(f"A new gathering must start as {DEFAULT_STATUS!r} (got {to_status!r}).")
	raise FlockGatheringError(f"Illegal gathering status transition: {from_status!r} -> {to_status!r}.")


# ---------------------------------------------------------------------------- #
# Branch-binding — a gathering is branch-bound to its group (ADR §4.2).
# ---------------------------------------------------------------------------- #
def validate_gathering_branch_binding(*, group_branch: str | None, gathering_branch: str) -> None:
	"""ADR §4.2 — a gathering's branch must match its group's branch.

	Raises :class:`FlockGatheringError` when a gathering declares a branch
	different from its group's branch (the gathering is branch-bound, exactly
	like the group subtree it lives under). ``group_branch`` may be ``None`` only
	when the group is missing/invalid — the caller (controller) supplies the
	group's resolved branch, so a ``None`` here means the binding cannot be
	verified and is rejected.
	"""
	if not gathering_branch:
		raise FlockGatheringError("Flock Gathering.branch is required (the row-level perm anchor).")
	if group_branch is None:
		raise FlockGatheringError("Flock Gathering.group has no branch — cannot bind the gathering.")
	if group_branch != gathering_branch:
		raise FlockGatheringError(
			"A gathering's branch must match its group's branch "
			f"(group branch {group_branch!r} != gathering branch {gathering_branch!r}). "
			"A gathering is branch-bound to its group (ADR §4.2)."
		)


# ---------------------------------------------------------------------------- #
# group_path — denormalized roll-up helper (ADR §9, FLO-6 §3.2).
# ---------------------------------------------------------------------------- #
def build_group_path(path_to_root: Sequence[str]) -> str:
	"""Build the denormalized ``group_path`` from a group's root-to-self chain.

	``path_to_root`` is ``[group, parent, ..., root]`` (the
	:func:`trees.path_to_root` order, self-first). The roll-up path is emitted
	root-first slash-delimited: ``/root/parent/group``. An empty chain yields
	``""`` (a gathering whose group could not be resolved — the controller
	rejects that before reaching here).
	"""
	if not path_to_root:
		return ""
	# trees.path_to_root is self-first; reverse to root-first for prefix roll-ups.
	return "/" + "/".join(reversed(path_to_root))


__all__ = (
	"DEFAULT_STATUS",
	"GATHERING_STATUSES",
	"STATUS_CANCELLED",
	"STATUS_CONFIRMED",
	"STATUS_HELD",
	"STATUS_REPORTED",
	"STATUS_SCHEDULED",
	"TERMINAL_STATUSES",
	"TRANSITIONS",
	"FlockGatheringError",
	"build_group_path",
	"is_terminal_status",
	"is_valid_transition",
	"validate_gathering_branch_binding",
	"validate_status_transition",
)
