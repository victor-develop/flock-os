"""
Framework-agnostic field-level validation rules for the people/membership layer
(FLO-5 §3.2 / §3.3, ADR-0001 §4.3).

Like :mod:`flock_os.flock_os.trees`, these predicates take primitives and never
touch the database, so they run under plain ``pytest`` without a Frappe site.
The Frappe DocType controllers fetch the relevant rows (parent group, existing
membership edges) via the framework and delegate the *decision* to these rules —
keeping the logic DRY, single-sourced, and unit-testable. DB-backed uniqueness is
checked by the controller using :func:`is_duplicate_pair` over the fetched set so
the composite-key logic itself is covered at project level.

Canonical choices honoured here (CEO-ratified, [FLO-27](/FLO/issues/FLO-27)):

- ``Flock Member`` is unique on ``(email, branch)`` — the same person may exist
  at two campuses as separate rows ([FLO-5](/FLO/issues/FLO-5) §8.3).
- ``Flock Group Member.branch`` is **denormalized** from ``group.branch`` in
  ``validate`` and is effectively read-only (Architect rec. #5) — it gives
  membership rows native branch isolation *and* the group-axis hook.
- ``Flock Group Member`` is unique on ``(group, member)``.
"""

from __future__ import annotations

from collections.abc import Iterable


class FlockMemberError(ValueError):
	"""Raised when a Flock Member field invariant is violated."""


class FlockGroupMemberError(ValueError):
	"""Raised when a Flock Group Member field invariant is violated."""


MEMBER_STATUS_OPTIONS: tuple[str, ...] = ("Member", "Pre-Member", "Visitor")
GROUP_MEMBER_ROLE_OPTIONS: tuple[str, ...] = ("Leader", "Co-Leader", "Member", "Visitor")
GROUP_MEMBER_STATUS_OPTIONS: tuple[str, ...] = ("Active", "Inactive")


def compute_member_full_name(*, first_name: str, last_name: str) -> str:
	"""Derive ``full_name`` from ``first_name`` / ``last_name`` (FLO-5 §3.2).

	``full_name`` is the canonical display/autoname-ish field. It is ``first_name``
	and ``last_name`` joined, with stray whitespace collapsed. A member must carry
	at least one of the two names.
	"""
	first = (first_name or "").strip()
	last = (last_name or "").strip()
	if not first and not last:
		raise FlockMemberError("Flock Member requires at least a first name or last name.")
	return " ".join(part for part in (first, last) if part)


def validate_member_status(status: str) -> None:
	"""A member ``status`` must be one of the canonical lifecycle states."""
	if status not in MEMBER_STATUS_OPTIONS:
		raise FlockMemberError(f"Flock Member.status {status!r} is not one of {MEMBER_STATUS_OPTIONS}.")


def validate_group_member_role(role: str) -> None:
	"""A membership ``role`` must be one of the canonical roster roles."""
	if role not in GROUP_MEMBER_ROLE_OPTIONS:
		raise FlockGroupMemberError(
			f"Flock Group Member.role {role!r} is not one of {GROUP_MEMBER_ROLE_OPTIONS}."
		)


def validate_group_member_status(status: str) -> None:
	"""A membership ``status`` must be Active or Inactive."""
	if status not in GROUP_MEMBER_STATUS_OPTIONS:
		raise FlockGroupMemberError(
			f"Flock Group Member.status {status!r} is not one of {GROUP_MEMBER_STATUS_OPTIONS}."
		)


def denormalize_group_member_branch(*, group_branch: str) -> str:
	"""Architect rec. #5 — ``Flock Group Member.branch`` mirrors ``group.branch``.

	Returns the branch a membership row must carry. Because group subtrees are
	branch-bound (see :mod:`flock_os.flock_os.trees`), this value is stable and
	gives the edge native branch User-Permission isolation alongside the
	group-tree axis hook.
	"""
	if not group_branch:
		raise FlockGroupMemberError(
			"Cannot denormalize Flock Group Member.branch: the linked group has no branch."
		)
	return group_branch


def validate_group_member_branch_matches(*, member_branch: str, group_branch: str) -> None:
	"""The denormalized ``branch`` on a membership row must equal ``group.branch``."""
	if member_branch != group_branch:
		raise FlockGroupMemberError(
			"Flock Group Member.branch must equal its group's branch "
			f"(member branch {member_branch!r} != group branch {group_branch!r})."
		)


def is_duplicate_pair(key: tuple[str, str], existing: Iterable[tuple[str, str]]) -> bool:
	"""Composite-key duplicate detector for ``(email, branch)`` / ``(group, member)``.

	The controller fetches the existing pairs for the relevant scope and asks this
	pure helper whether ``key`` already occurs — so the composite-uniqueness rule
	is unit-tested at project level without a database.
	"""
	return key in tuple(existing)
