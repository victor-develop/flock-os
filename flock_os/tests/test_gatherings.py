"""
Project-level tests for :mod:`flock_os.gatherings` — the pure domain half of
``Flock Gathering`` ([FLO-54](/FLO/issues/FLO-54), spec [FLO-6](/FLO/issues/FLO-6)
§3.2/§4, ADR-0001 §3/§4.2/§9).

These run under plain ``pytest`` (no Frappe site / bench required). They pin the
reporting state machine, the branch-binding invariant, and the ``group_path``
denormalization — the invariants the DocType controller enforces on every save.
The Frappe-coupled half (the ``validate`` hook, DB lookups) is exercised by the
bench-level integration test ``doctype/flock_gathering/test_flock_gathering.py``;
the cross-branch row-scope contract is pinned in :mod:`flock_os.tests.test_permissions`
+ :mod:`flock_os.tests.test_doctype_schema`.
"""

from __future__ import annotations

import pytest

from flock_os import gatherings

# --------------------------------------------------------------------------- #
# Reporting state machine (FLO-6 §4).
# --------------------------------------------------------------------------- #


def test_status_catalog_matches_spec_order():
	# FLO-6 §4 lifecycle: Scheduled -> Held -> Reported -> Confirmed; Cancelled
	# terminal. The ordered Select options on Flock Gathering.status must match.
	assert gatherings.GATHERING_STATUSES == (
		"Scheduled",
		"Held",
		"Reported",
		"Confirmed",
		"Cancelled",
	)
	assert gatherings.DEFAULT_STATUS == "Scheduled"


def test_new_gathering_must_start_scheduled():
	# from_status=None models a brand-new gathering: only Scheduled is legal.
	assert gatherings.is_valid_transition(from_status=None, to_status="Scheduled")
	for forged in ("Held", "Reported", "Confirmed", "Cancelled"):
		assert not gatherings.is_valid_transition(from_status=None, to_status=forged), (
			f"a new gathering must not forge {forged!r}"
		)


@pytest.mark.parametrize(
	("from_status", "to_status"),
	[
		("Scheduled", "Held"),
		("Held", "Reported"),
		("Reported", "Confirmed"),
		("Reported", "Held"),  # reject / reopen
		("Scheduled", "Cancelled"),
		("Held", "Cancelled"),
	],
)
def test_legal_transitions_allowed(from_status, to_status):
	assert gatherings.is_valid_transition(from_status=from_status, to_status=to_status)


@pytest.mark.parametrize(
	("from_status", "to_status"),
	[
		# No skipping the reporting lifecycle.
		("Scheduled", "Reported"),
		("Scheduled", "Confirmed"),
		("Held", "Confirmed"),
		# No backwards wildcards.
		("Confirmed", "Held"),
		("Held", "Scheduled"),
		# Cancelled is terminal.
		("Cancelled", "Scheduled"),
		("Cancelled", "Held"),
	],
)
def test_illegal_transitions_rejected(from_status, to_status):
	assert not gatherings.is_valid_transition(from_status=from_status, to_status=to_status)


def test_same_status_resave_is_allowed():
	# Re-saving in the same status is a no-op transition (always allowed).
	for status in gatherings.GATHERING_STATUSES:
		assert gatherings.is_valid_transition(from_status=status, to_status=status)


def test_terminal_statuses_block_forward_moves():
	assert set(gatherings.TERMINAL_STATUSES) == {"Confirmed", "Cancelled"}
	for status in gatherings.TERMINAL_STATUSES:
		assert gatherings.is_terminal_status(status)
		assert gatherings.TRANSITIONS[status] == frozenset()


def test_unknown_status_rejected():
	assert not gatherings.is_valid_transition(from_status="Scheduled", to_status="Bogus")
	assert not gatherings.is_valid_transition(from_status=None, to_status="Bogus")


def test_validate_status_transition_raises_for_illegal_move():
	# A new gathering forging Confirmed raises with a clear new-doc message.
	with pytest.raises(gatherings.FlockGatheringError, match="new gathering"):
		gatherings.validate_status_transition(from_status=None, to_status="Confirmed")
	# An illegal existing-doc move raises with a transition message.
	with pytest.raises(gatherings.FlockGatheringError, match="Illegal"):
		gatherings.validate_status_transition(from_status="Held", to_status="Confirmed")


def test_validate_status_transition_allows_legal_move():
	# No exception for a legal forward move.
	gatherings.validate_status_transition(from_status="Scheduled", to_status="Held")


# --------------------------------------------------------------------------- #
# Branch-binding — a gathering is branch-bound to its group (ADR §4.2).
# --------------------------------------------------------------------------- #


def test_branch_binding_passes_when_branches_match():
	gatherings.validate_gathering_branch_binding(group_branch="North", gathering_branch="North")


def test_branch_binding_rejects_mismatched_branch():
	with pytest.raises(gatherings.FlockGatheringError, match="must match"):
		gatherings.validate_gathering_branch_binding(group_branch="North", gathering_branch="South")


def test_branch_binding_rejects_empty_gathering_branch():
	# branch is the row-level perm anchor — it must be present.
	with pytest.raises(gatherings.FlockGatheringError, match="required"):
		gatherings.validate_gathering_branch_binding(group_branch="North", gathering_branch="")


def test_branch_binding_rejects_group_without_branch():
	# A group with no branch cannot bind the gathering (caller resolves None).
	with pytest.raises(gatherings.FlockGatheringError, match="no branch"):
		gatherings.validate_gathering_branch_binding(group_branch=None, gathering_branch="North")


# --------------------------------------------------------------------------- #
# group_path — denormalized roll-up helper (ADR §9, no permission semantics).
# --------------------------------------------------------------------------- #


def test_group_path_is_root_first_slash_delimited():
	# trees.path_to_root is self-first [group, parent, root]; the roll-up path is
	# emitted root-first so a subtree roll-up can LIKE '/<root>/%'.
	# path_to_root = ["Youth-Band", "Youth", "North-Ministries"] (self-first).
	path = gatherings.build_group_path(["Youth-Band", "Youth", "North-Ministries"])
	assert path == "/North-Ministries/Youth/Youth-Band"


def test_group_path_subtree_prefix_matches_descendants():
	# A root group's path prefixes every descendant's path — the LIKE roll-up
	# contract (ADR §9). '/North-Ministries/%' matches the descendant below.
	root = gatherings.build_group_path(["North-Ministries"])
	descendant = gatherings.build_group_path(["Youth-Band", "Youth", "North-Ministries"])
	assert descendant.startswith(root + "/")
	assert root == "/North-Ministries"


def test_group_path_empty_chain_yields_empty_string():
	assert gatherings.build_group_path([]) == ""
