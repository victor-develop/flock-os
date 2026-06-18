"""
Project-level tests for the framework-agnostic validation core (FLO-17).

These run under plain ``pytest`` (no Frappe site / bench required) and pin the
pure domain rules that the Frappe DocType controllers delegate to:
``flock_os.flock_os.trees`` (group-branch binding) and
``flock_os.flock_os.rules`` (member / membership field invariants), plus the
core-DocType → canonical-event wiring in :mod:`flock_os.hooks` (which targets
the single sanctioned emitter :mod:`flock_os.events` shipped by FLO-14).
"""

from __future__ import annotations

import pytest

import flock_os.hooks as hooks
from flock_os import events as flock_events
from flock_os.flock_os import rules, trees

# --------------------------------------------------------------------------- #
# Group-branch binding (trees.validate_group_branch_binding) — ADR §4.2
# --------------------------------------------------------------------------- #


def test_root_group_may_set_any_branch():
	# A root group (no parent) sets the branch for its subtree — always allowed.
	trees.validate_group_branch_binding(parent_branch=None, child_branch="Campus A")


def test_child_group_inheriting_parent_branch_is_valid():
	trees.validate_group_branch_binding(parent_branch="Campus A", child_branch="Campus A")


def test_child_group_with_divergent_branch_is_rejected():
	with pytest.raises(trees.FlockTreeError):
		trees.validate_group_branch_binding(parent_branch="Campus A", child_branch="Campus B")


def test_group_without_branch_is_rejected():
	# branch is the row-level permission anchor — it is mandatory.
	with pytest.raises(trees.FlockTreeError):
		trees.validate_group_branch_binding(parent_branch=None, child_branch="")


# --------------------------------------------------------------------------- #
# Member field rules (rules.*) — FLO-5 §3.2
# --------------------------------------------------------------------------- #


def test_full_name_joins_first_and_last():
	assert rules.compute_member_full_name(first_name="Grace", last_name="Lee") == "Grace Lee"


def test_full_name_collapses_missing_parts():
	assert rules.compute_member_full_name(first_name="Grace", last_name="") == "Grace"
	assert rules.compute_member_full_name(first_name="  ", last_name="Lee") == "Lee"


def test_full_name_requires_a_name():
	with pytest.raises(rules.FlockMemberError):
		rules.compute_member_full_name(first_name="", last_name="")


@pytest.mark.parametrize("status", rules.MEMBER_STATUS_OPTIONS)
def test_member_status_accepts_canonical(status):
	rules.validate_member_status(status)


def test_member_status_rejects_unknown():
	with pytest.raises(rules.FlockMemberError):
		rules.validate_member_status("Bogus")


# --------------------------------------------------------------------------- #
# Group Member edge rules (rules.*) — FLO-5 §3.3 (denormalization rec #5)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("role", rules.GROUP_MEMBER_ROLE_OPTIONS)
def test_group_member_role_accepts_canonical(role):
	rules.validate_group_member_role(role)


def test_group_member_role_rejects_unknown():
	with pytest.raises(rules.FlockGroupMemberError):
		rules.validate_group_member_role("Captain")


def test_group_member_status_rejects_unknown():
	with pytest.raises(rules.FlockGroupMemberError):
		rules.validate_group_member_status("Pending")


def test_denormalize_branch_mirrors_group_branch():
	assert rules.denormalize_group_member_branch(group_branch="Campus A") == "Campus A"


def test_denormalize_branch_requires_group_branch():
	with pytest.raises(rules.FlockGroupMemberError):
		rules.denormalize_group_member_branch(group_branch="")


def test_group_member_branch_must_match_group_branch():
	# The denormalized branch is validated against the group's branch.
	rules.validate_group_member_branch_matches(member_branch="Campus A", group_branch="Campus A")
	with pytest.raises(rules.FlockGroupMemberError):
		rules.validate_group_member_branch_matches(member_branch="Campus A", group_branch="Campus B")


# --------------------------------------------------------------------------- #
# Composite-key uniqueness helpers (rules.is_duplicate_pair) — FLO-5 §3.2/§3.3
# --------------------------------------------------------------------------- #


def test_is_duplicate_pair_detects_existing_email_branch():
	existing = [("sam@x.org", "A"), ("ada@x.org", "A")]
	assert rules.is_duplicate_pair(("sam@x.org", "A"), existing) is True
	assert rules.is_duplicate_pair(("sam@x.org", "B"), existing) is False


def test_is_duplicate_pair_detects_existing_group_member():
	existing = [("G1", "M1"), ("G1", "M2")]
	assert rules.is_duplicate_pair(("G1", "M1"), existing) is True
	assert rules.is_duplicate_pair(("G1", "M3"), existing) is False


# --------------------------------------------------------------------------- #
# Core-DocType → canonical event wiring (hooks + flock_os.events catalog) — ADR §5.4
# --------------------------------------------------------------------------- #


def test_core_doc_events_target_the_canonical_catalog():
	# FLO-17 wires the core DocTypes to the single sanctioned emitter
	# (`flock_os.events`, shipped by FLO-14). Every mapped event must exist in the
	# canonical catalog and follow the flock.<aggregate>.<verb-past> convention.
	catalog = {
		value
		for name, value in vars(flock_events).items()
		if not name.startswith("_") and isinstance(value, str) and value.startswith("flock.")
	}
	assert catalog
	aggregate_by_doctype = {
		"Flock Branch": "branch",
		"Flock Group": "group",
		"Flock Group Member": "group_member",
		"Flock Member": "member",
		"Flock Gathering": "gathering",
		"Flock Announcement": "announcement",
	}
	assert hooks._FLOCK_DOC_EVENTS  # wired
	for (doctype, hook), event in hooks._FLOCK_DOC_EVENTS.items():
		assert event in catalog, f"{doctype} wires uncataloged event {event!r}"
		assert event.startswith("flock.")
		assert doctype.startswith("Flock ")
		# catalog name: flock.<aggregate>.<verb-past>; aggregate tracks the doctype
		aggregate = event.removeprefix("flock.").split(".")[0]
		assert aggregate_by_doctype[doctype] == aggregate, (
			f"{doctype} aggregate {aggregate!r} != expected {aggregate_by_doctype[doctype]!r}"
		)
		assert hook in {"after_insert", "on_update"}


def test_doc_events_dict_points_at_the_dispatcher():
	# Frappe resolves these string paths; they must land on the dispatcher.
	for lifecycle in hooks.doc_events.values():
		for fn_path in lifecycle.values():
			assert fn_path == "flock_os.hooks._dispatch_flock_doc_event"


def test_dispatcher_resolves_to_public_emitter():
	# The dispatcher is a thin bridge to flock_os.events.on_doc_event (DRY: scope
	# + payload derivation live in the canonical emitter, not duplicated here).
	assert callable(hooks._dispatch_flock_doc_event)
	assert callable(flock_events.on_doc_event)
