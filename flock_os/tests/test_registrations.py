"""
Project-level tests for scoped one-time-event registration (FLO-7 §3.5 / §5 /
§6.2 / §7, materialized by [FLO-62]).

These run under plain ``pytest`` (no Frappe site / bench required). The pure
halves of scoped registration — the scope/status catalogs, the registration
state machine, the eligibility predicate (:func:`is_member_in_scope`), the
window + capacity decisions, the Flock Event Registration schema contract, and
the ``flock.registration.*`` event catalog — are exercised against in-memory
recording gateways so every rule in FLO-7 §5 is pinned without a database.
Same hexagonal-port discipline as :mod:`flock_os.tests.test_approvals`.

Coverage map (FLO-7):

* §3.5 registration status catalog + state machine.
* §5 eligibility — :func:`is_member_in_scope` (Own Group / Group Subtree /
  Branch / Branch Subtree / Org-wide in + out; Invited Only Phase B; None
  closed; unknown scope raises; empty inputs fail closed).
* §5 window — :func:`is_registration_window_open`.
* §5 capacity — :func:`capacity_decision` / :func:`is_capacity_full`.
* §5 composite gate — :func:`is_gathering_registration_eligible`.
* §6.2 / §3.5 schema + scoping contract — Flock Event Registration fields,
  SCOPED_DOCTYPES + MEMBER_ANCHORED_DOCTYPES registration, the v0_2 index
  patch contract.
* §7 event catalog — ``flock.registration.*`` events emit via the canonical bus.

The Frappe-coupled controller (doc lifecycle, atomic capacity UPDATE, REST
actions, the approval→registration write-back) is integration-tested via
``bench run-tests``; this gate asserts the domain layer is correct and the
scope/schema contract holds.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from flock_os import events, registrations
from flock_os import permissions as perms
from flock_os.registrations import (
	DEFAULT_REGISTRATION_SCOPE,
	DEFAULT_REGISTRATION_STATUS,
	REGISTRATION_CANCELLED,
	REGISTRATION_CHECKED_IN,
	REGISTRATION_NO_SHOW,
	REGISTRATION_REGISTERED,
	REGISTRATION_SCOPES,
	REGISTRATION_STATUSES,
	REGISTRATION_WAITLISTED,
	SCOPE_BRANCH,
	SCOPE_BRANCH_SUBTREE,
	SCOPE_GROUP_SUBTREE,
	SCOPE_INVITED_ONLY,
	SCOPE_NONE,
	SCOPE_ORG_WIDE,
	SCOPE_OWN_GROUP,
	RegistrationScopeGateway,
	RegistrationWindow,
)

DOCTYPE_DIR = Path(__file__).resolve().parent.parent / "flock_os" / "doctype"


# --------------------------------------------------------------------------- #
# In-memory scope world (FLO-7 §5). Two branches under one org:
#
#   Org "O"
#    ├── Branch "North"  ── gathering "G_N" lives here, group "G2"
#    │    └── Branch "North-Sub"  (a child branch, for Branch Subtree scope)
#    └── Branch "South"  ── gathering "G_S" lives here
#
#   Groups under North: G1 (parent) ⊃ G2 (the gathering's group). Member "M_in"
#   belongs to G2; "M_sub" belongs to G1 (subtree of G2's root via the group
#   tree); "M_other" is in South.
# --------------------------------------------------------------------------- #
@dataclass
class RecordingRegistrationScopeGateway(RegistrationScopeGateway):
	"""In-memory :class:`RegistrationScopeGateway` for deterministic scope tests."""

	branch_by_member: dict[str, str] = field(default_factory=dict)
	org_by_branch: dict[str, str] = field(default_factory=dict)
	groups_by_member: dict[str, tuple[str, ...]] = field(default_factory=dict)
	group_subtree_by_group: dict[str, tuple[str, ...]] = field(default_factory=dict)
	branch_subtree_by_branch: dict[str, tuple[str, ...]] = field(default_factory=dict)
	branch_by_gathering: dict[str, str] = field(default_factory=dict)
	org_by_gathering: dict[str, str] = field(default_factory=dict)
	group_by_gathering: dict[str, str] = field(default_factory=dict)

	def member_branch(self, member: str) -> str | None:
		return self.branch_by_member.get(member)

	def member_organization(self, member: str) -> str | None:
		branch = self.member_branch(member)
		return self.org_by_branch.get(branch) if branch else None

	def member_groups(self, member: str) -> tuple[str, ...]:
		return self.groups_by_member.get(member, ())

	def group_subtree(self, group: str) -> tuple[str, ...]:
		return self.group_subtree_by_group.get(group, ())

	def branch_subtree(self, branch: str) -> tuple[str, ...]:
		return self.branch_subtree_by_branch.get(branch, ())

	def gathering_branch(self, gathering: str) -> str | None:
		return self.branch_by_gathering.get(gathering)

	def gathering_organization(self, gathering: str) -> str | None:
		return self.org_by_gathering.get(gathering)

	def gathering_group(self, gathering: str) -> str | None:
		return self.group_by_gathering.get(gathering)


def _world() -> RecordingRegistrationScopeGateway:
	"""The default two-branch North/South world; gathering G_N in North/G2."""
	return RecordingRegistrationScopeGateway(
		branch_by_member={"M_in": "North", "M_sub": "North", "M_other": "South", "M_root": "North-Sub"},
		org_by_branch={"North": "O", "South": "O", "North-Sub": "O"},
		groups_by_member={"M_in": ("G2",), "M_sub": ("G1",), "M_other": ("G_S",)},
		group_subtree_by_group={"G2": ("G2", "G1")},  # G2 + its ancestor/group subtree
		branch_subtree_by_branch={"North": ("North", "North-Sub")},
		branch_by_gathering={"G_N": "North", "G_S": "South"},
		org_by_gathering={"G_N": "O", "G_S": "O"},
		group_by_gathering={"G_N": "G2", "G_S": "G_S"},
	)


def _eligible(
	member: str = "M_in",
	gathering: str = "G_N",
	scope: str = SCOPE_OWN_GROUP,
	**overrides: Any,
) -> bool:
	gw = _world()
	for key, value in overrides.items():
		setattr(gw, key, value)
	return registrations.is_member_in_scope(member=member, gathering=gathering, scope=scope, gateway=gw)


# --------------------------------------------------------------------------- #
# Catalogs (FLO-7 §3.5 / §5)
# --------------------------------------------------------------------------- #
def test_scope_catalog_matches_spec():
	# §5 Select options (mirrors Flock Event Approval.proposed_registration_scope).
	assert REGISTRATION_SCOPES == (
		SCOPE_NONE,
		SCOPE_OWN_GROUP,
		SCOPE_GROUP_SUBTREE,
		SCOPE_BRANCH,
		SCOPE_BRANCH_SUBTREE,
		SCOPE_ORG_WIDE,
		SCOPE_INVITED_ONLY,
	)
	assert DEFAULT_REGISTRATION_SCOPE == SCOPE_OWN_GROUP
	assert registrations.CLOSED_SCOPES == frozenset({SCOPE_NONE})


def test_status_catalog_matches_spec():
	# §3.5 Select options.
	assert REGISTRATION_STATUSES == (
		REGISTRATION_REGISTERED,
		REGISTRATION_WAITLISTED,
		REGISTRATION_CANCELLED,
		REGISTRATION_CHECKED_IN,
		REGISTRATION_NO_SHOW,
	)
	assert DEFAULT_REGISTRATION_STATUS == REGISTRATION_REGISTERED
	assert registrations.INACTIVE_REGISTRATION_STATUSES == frozenset(
		{REGISTRATION_CANCELLED, REGISTRATION_NO_SHOW}
	)


def test_via_catalog_matches_spec():
	assert registrations.REGISTRATION_VIA == (
		registrations.VIA_SELF,
		registrations.VIA_LEADER,
		registrations.VIA_INVITE,
		registrations.VIA_BULK,
	)
	assert registrations.DEFAULT_REGISTRATION_VIA == registrations.VIA_SELF


# --------------------------------------------------------------------------- #
# Eligibility predicate (FLO-7 §5) — the heart of "out-of-scope rejected".
# --------------------------------------------------------------------------- #
def test_own_group_member_is_in_scope():
	assert _eligible(member="M_in", scope=SCOPE_OWN_GROUP) is True


def test_own_group_non_member_is_out_of_scope():
	# M_sub belongs to G1, not the gathering's own group G2.
	assert _eligible(member="M_sub", scope=SCOPE_OWN_GROUP) is False


def test_group_subtree_includes_ancestor_group_members():
	# G2's subtree is (G2, G1); M_sub belongs to G1 → in scope.
	assert _eligible(member="M_sub", scope=SCOPE_GROUP_SUBTREE) is True


def test_group_subtree_excludes_unrelated_member():
	assert _eligible(member="M_other", scope=SCOPE_GROUP_SUBTREE) is False


def test_branch_scope_matches_gathering_branch():
	# M_in's home branch is North; gathering G_N is in North.
	assert _eligible(member="M_in", scope=SCOPE_BRANCH) is True
	# M_other is in South; gathering is in North → out of scope.
	assert _eligible(member="M_other", scope=SCOPE_BRANCH) is False


def test_branch_subtree_includes_descendant_branch_members():
	# North's subtree is (North, North-Sub); M_root (North-Sub) is in scope.
	assert _eligible(member="M_root", scope=SCOPE_BRANCH_SUBTREE) is True
	# South is not in North's subtree.
	assert _eligible(member="M_other", scope=SCOPE_BRANCH_SUBTREE) is False


def test_org_wide_admits_any_org_member():
	# Both branches are under Org "O".
	assert _eligible(member="M_other", scope=SCOPE_ORG_WIDE) is True


def test_org_wide_rejects_other_org_member():
	gw = _world()
	gw.org_by_branch["South"] = "OtherOrg"  # M_other now in a different org
	assert (
		registrations.is_member_in_scope(member="M_other", gathering="G_N", scope=SCOPE_ORG_WIDE, gateway=gw)
		is False
	)


def test_invited_only_is_phase_b_and_fails_closed():
	# Invitations are Phase B ([FLO-79]); until they land nothing is in scope.
	assert _eligible(member="M_in", scope=SCOPE_INVITED_ONLY) is False


def test_none_scope_is_closed():
	assert _eligible(member="M_in", scope=SCOPE_NONE) is False


def test_empty_inputs_fail_closed():
	gw = _world()
	assert (
		registrations.is_member_in_scope(member="", gathering="G_N", scope=SCOPE_OWN_GROUP, gateway=gw)
		is False
	)
	assert (
		registrations.is_member_in_scope(member="M_in", gathering="", scope=SCOPE_OWN_GROUP, gateway=gw)
		is False
	)


def test_unknown_scope_raises_rather_than_silently_admitting():
	gw = _world()
	with pytest.raises(registrations.FlockRegistrationError):
		registrations.is_member_in_scope(member="M_in", gathering="G_N", scope="Galaxy-wide", gateway=gw)


def test_null_gateway_yields_no_scope_before_wiring():
	null = registrations.NullRegistrationScopeGateway()
	assert isinstance(null, RegistrationScopeGateway)
	# Nothing resolves → every scope predicate fails closed.
	for scope in (SCOPE_OWN_GROUP, SCOPE_BRANCH, SCOPE_ORG_WIDE):
		assert (
			registrations.is_member_in_scope(member="M_in", gathering="G_N", scope=scope, gateway=null)
			is False
		)


# --------------------------------------------------------------------------- #
# Eligibility reason (FLO-7 §8 — the read-only UI hint).
# --------------------------------------------------------------------------- #
def test_eligibility_reason_in_scope():
	gw = _world()
	reason = registrations.eligibility_reason(
		member="M_in", gathering="G_N", scope=SCOPE_OWN_GROUP, gateway=gw
	)
	assert "eligible" in reason
	assert "Own Group" in reason


def test_eligibility_reason_out_of_scope():
	gw = _world()
	reason = registrations.eligibility_reason(
		member="M_other", gathering="G_N", scope=SCOPE_BRANCH, gateway=gw
	)
	assert "out of scope" in reason


def test_eligibility_reason_closed_and_invited():
	gw = _world()
	assert "closed" in registrations.eligibility_reason(
		member="M_in", gathering="G_N", scope=SCOPE_NONE, gateway=gw
	)
	assert "Invited Only" in registrations.eligibility_reason(
		member="M_in", gathering="G_N", scope=SCOPE_INVITED_ONLY, gateway=gw
	)


# --------------------------------------------------------------------------- #
# Window predicate (FLO-7 §5 #1).
# --------------------------------------------------------------------------- #
def _window(**overrides: Any) -> RegistrationWindow:
	base = dict(
		approval_status="Approved",
		scope=SCOPE_OWN_GROUP,
		opens_on=None,
		closes_on=None,
		capacity=None,
		registered_count=0,
	)
	base.update(overrides)
	return RegistrationWindow(**base)


def test_window_open_when_approved_and_unbounded():
	assert registrations.is_registration_window_open(_window(), now="2026-06-20 10:00:00") is True


def test_window_closed_when_scope_is_none():
	assert (
		registrations.is_registration_window_open(_window(scope=SCOPE_NONE), now="2026-06-20 10:00:00")
		is False
	)


@pytest.mark.parametrize("status", ["Not Required", "Draft", "Pending Approval", "Rejected", "Cancelled"])
def test_window_closed_until_final_approval(status):
	# §4.2 #2: registration is gated on the gathering being Approved.
	assert (
		registrations.is_registration_window_open(_window(approval_status=status), now="2026-06-20 10:00:00")
		is False
	)


def test_window_respects_opens_on():
	w = _window(opens_on="2026-06-21 00:00:00")
	assert registrations.is_registration_window_open(w, now="2026-06-20 10:00:00") is False
	assert registrations.is_registration_window_open(w, now="2026-06-21 00:00:01") is True


def test_window_respects_closes_on():
	w = _window(closes_on="2026-06-19 23:59:59")
	assert registrations.is_registration_window_open(w, now="2026-06-20 10:00:00") is False
	assert registrations.is_registration_window_open(w, now="2026-06-19 10:00:00") is True


# --------------------------------------------------------------------------- #
# Capacity decision (FLO-7 §5 #3).
# --------------------------------------------------------------------------- #
def test_capacity_uncapped_when_null_or_zero():
	# null capacity → uncapped, always seated.
	assert registrations.capacity_decision(capacity=None, registered_count=9999).seated is True
	# zero capacity → uncapped (the spec treats 0 as uncapped, not 0-seated).
	assert registrations.capacity_decision(capacity=0, registered_count=5).seated is True


def test_capacity_seated_below_cap():
	decision = registrations.capacity_decision(capacity=100, registered_count=50)
	assert decision.seated is True
	assert decision.status == REGISTRATION_REGISTERED
	assert decision.is_waitlisted is False


def test_capacity_waitlisted_at_cap():
	decision = registrations.capacity_decision(capacity=100, registered_count=100)
	assert decision.seated is False
	assert decision.status == REGISTRATION_WAITLISTED
	assert decision.is_waitlisted is True


def test_is_capacity_full():
	assert registrations.is_capacity_full(capacity=100, registered_count=100) is True
	assert registrations.is_capacity_full(capacity=100, registered_count=99) is False
	assert registrations.is_capacity_full(capacity=None, registered_count=99999) is False


# --------------------------------------------------------------------------- #
# Composite eligibility gate (§5 #1–#2).
# --------------------------------------------------------------------------- #
def test_composite_gate_requires_window_and_scope():
	gw = _world()
	now = "2026-06-20 10:00:00"
	# Approved + in scope → eligible.
	assert (
		registrations.is_gathering_registration_eligible(
			window=_window(), now=now, member="M_in", gathering="G_N", gateway=gw
		)
		is True
	)
	# Out of scope → not eligible even when window is open.
	assert (
		registrations.is_gathering_registration_eligible(
			window=_window(), now=now, member="M_other", gathering="G_N", gateway=gw
		)
		is False
	)
	# Window closed → not eligible even when in scope.
	assert (
		registrations.is_gathering_registration_eligible(
			window=_window(approval_status="Pending Approval"),
			now=now,
			member="M_in",
			gathering="G_N",
			gateway=gw,
		)
		is False
	)


# --------------------------------------------------------------------------- #
# Registration state machine (FLO-7 §3.5).
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
	("from_status", "to_status"),
	[
		(REGISTRATION_REGISTERED, REGISTRATION_CHECKED_IN),
		(REGISTRATION_REGISTERED, REGISTRATION_CANCELLED),
		(REGISTRATION_WAITLISTED, REGISTRATION_REGISTERED),
		(REGISTRATION_WAITLISTED, REGISTRATION_CANCELLED),
		(REGISTRATION_REGISTERED, REGISTRATION_REGISTERED),  # no-op
	],
)
def test_valid_registration_transitions(from_status, to_status):
	assert registrations.is_valid_registration_transition(from_status=from_status, to_status=to_status)


@pytest.mark.parametrize(
	("from_status", "to_status"),
	[
		(REGISTRATION_CANCELLED, REGISTRATION_REGISTERED),  # terminal
		(REGISTRATION_CHECKED_IN, REGISTRATION_CANCELLED),  # terminal
		(REGISTRATION_REGISTERED, REGISTRATION_WAITLISTED),  # not a legal move
		(REGISTRATION_NO_SHOW, REGISTRATION_REGISTERED),  # terminal
	],
)
def test_invalid_registration_transitions(from_status, to_status):
	assert not registrations.is_valid_registration_transition(from_status=from_status, to_status=to_status)


def test_validate_registration_transition_raises_on_illegal_move():
	with pytest.raises(registrations.FlockRegistrationError):
		registrations.validate_registration_transition(
			from_status=REGISTRATION_CANCELLED, to_status=REGISTRATION_REGISTERED
		)
	# A legal move does not raise.
	registrations.validate_registration_transition(
		from_status=REGISTRATION_REGISTERED, to_status=REGISTRATION_CHECKED_IN
	)


def test_terminal_registration_statuses():
	assert registrations.is_terminal_registration_status(REGISTRATION_CANCELLED) is True
	assert registrations.is_terminal_registration_status(REGISTRATION_CHECKED_IN) is True
	assert registrations.is_terminal_registration_status(REGISTRATION_REGISTERED) is False


# --------------------------------------------------------------------------- #
# Event catalog (FLO-7 §7) — registration events emit via the canonical bus.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
	"event_name",
	[
		events.REGISTRATION_OPENED,
		events.REGISTRATION_CREATED,
		events.REGISTRATION_WAITLISTED,
		events.REGISTRATION_CANCELLED,
		events.REGISTRATION_CHECKED_IN,
	],
)
def test_registration_events_emit_via_canonical_bus(event_name):
	sink = events.RecordingEventSink()
	bus = events.EventBus(sink)
	bus.emit(event_name, payload={"gathering": "GATH-1"}, scope={"branch": "North"})
	published = [ev.name for ev, _, _ in sink.published]
	assert event_name in published


def test_registration_event_names_are_flock_prefixed():
	for name in (
		events.REGISTRATION_OPENED,
		events.REGISTRATION_CREATED,
		events.REGISTRATION_WAITLISTED,
		events.REGISTRATION_CANCELLED,
		events.REGISTRATION_CHECKED_IN,
	):
		assert name.startswith("flock.registration.")


# --------------------------------------------------------------------------- #
# Schema contract — Flock Event Registration DocType (FLO-7 §3.5 / §6.2).
# --------------------------------------------------------------------------- #
def _load_doctype(name: str) -> dict:
	path = DOCTYPE_DIR / name.lower().replace(" ", "_") / f"{name.lower().replace(' ', '_')}.json"
	assert path.exists(), f"Missing DocType JSON for {name}: {path}"
	with path.open() as f:
		return json.load(f)


def _field(doc: dict, fieldname: str) -> dict:
	matches = [f for f in doc["fields"] if f["fieldname"] == fieldname]
	assert matches, f"field {fieldname!r} missing"
	return matches[0]


def test_flock_event_registration_doctype_identity():
	doc = _load_doctype("Flock Event Registration")
	assert doc["doctype"] == "DocType"
	assert doc["name"] == "Flock Event Registration"
	assert doc["module"] == "flock_os"
	assert doc["engine"] == "InnoDB"
	# Top-level transaction (NOT a child table) — scales to 15k (§3.5).
	assert doc.get("is_submittable", 0) == 0
	assert doc["autoname"] == "naming_series:"


def test_flock_event_registration_scoping_contract():
	doc = _load_doctype("Flock Event Registration")
	branch = _field(doc, "branch")
	assert branch["fieldtype"] == "Link"
	assert branch["options"] == "Flock Branch"
	assert branch.get("reqd") == 1
	assert branch.get("search_index") == 1
	group = _field(doc, "group")
	assert group["options"] == "Flock Group"
	assert group.get("reqd") == 1
	assert group.get("search_index") == 1
	assert _field(doc, "organization")["options"] == "Flock Organization"


def test_flock_event_registration_fields_match_spec():
	doc = _load_doctype("Flock Event Registration")
	# §3.5 fields.
	assert _field(doc, "gathering")["options"] == "Flock Gathering"
	assert _field(doc, "gathering").get("reqd") == 1
	registrant = _field(doc, "registrant")
	assert registrant["options"] == "Flock Member"
	assert registrant.get("reqd") == 1
	# registration_status Select options match the catalog.
	status = _field(doc, "registration_status")
	assert status["fieldtype"] == "Select"
	assert status["options"].split("\n") == list(REGISTRATION_STATUSES)
	# registered_via Select.
	assert _field(doc, "registered_via")["options"].split("\n") == list(registrations.REGISTRATION_VIA)
	# Bridge + provenance fields.
	for fld in (
		"registrant_name",
		"registered_at",
		"checked_in_attendance",
		"invite",
		"metadata",
	):
		_field(doc, fld)


def test_flock_event_registration_query_indexes():
	# §3.5 hot-path indexes for capacity/waitlist + branch-scoped reads.
	doc = _load_doctype("Flock Event Registration")
	for fld in ("gathering", "registrant", "branch", "registration_status"):
		assert _field(doc, fld).get("search_index") == 1, f"{fld} must be indexed"


def test_flock_event_registration_role_permissions():
	# §6.1: Member creates/edits their own row; Leader/BA/OrgAdmin manage in
	# scope; Auditor reads.
	doc = _load_doctype("Flock Event Registration")
	by_role: dict[str, list[dict]] = {}
	for p in doc["permissions"]:
		by_role.setdefault(p["role"], []).append(p)
	assert "System Manager" in by_role
	assert any(p.get("create") for p in by_role.get("Flock Member", []))
	assert any(p.get("create") for p in by_role.get("Flock Group Leader", []))
	assert any(p.get("write") for p in by_role.get("Flock Branch Admin", []))
	# Auditor reads, never writes (compliance).
	for p in by_role.get("Flock Auditor", []):
		assert p.get("read") == 1
		assert p.get("write", 0) == 0


# --------------------------------------------------------------------------- #
# Flock Gathering registration extension fields (FLO-7 §3.1).
# --------------------------------------------------------------------------- #
def test_flock_gathering_has_registration_extension_fields():
	doc = _load_doctype("Flock Gathering")
	scope = _field(doc, "registration_scope")
	assert scope["fieldtype"] == "Select"
	assert scope["options"].split("\n") == list(REGISTRATION_SCOPES)
	for fld in (
		"registration_capacity",
		"registration_opens_on",
		"registration_closes_on",
		"registered_count",
		"checked_in_count",
	):
		_field(doc, fld)


def test_flock_gathering_drops_stored_is_registration_open():
	# `is_registration_open` (FLO-7 §3.1) is time-dependent (Approved AND
	# now-in-window AND registered_count < capacity) and cannot be stored
	# correctly — a saved value is stale the instant it lands. The authoritative
	# live hint is `get_registration_eligibility` (which computes it at request
	# time); a stored/onload badge is a Phase B frontend concern ([FLO-79]).
	# Dropping the stored field keeps the UI from showing a perpetually-false
	# hint (architect review, FLO-62).
	doc = _load_doctype("Flock Gathering")
	assert not [f for f in doc["fields"] if f["fieldname"] == "is_registration_open"]


def test_flock_gathering_registration_counters_are_permlevel_2_readonly():
	doc = _load_doctype("Flock Gathering")
	for fld in ("registered_count", "checked_in_count"):
		counter = _field(doc, fld)
		assert counter["fieldtype"] == "Int"
		assert counter.get("read_only") == 1
		assert counter.get("permlevel") == 2
		assert counter.get("non_negative") == 1


def test_flock_gathering_registration_scope_is_permlevel_1():
	# §6.3: leader proposes (permlevel 1); final approver confirms.
	doc = _load_doctype("Flock Gathering")
	assert _field(doc, "registration_scope").get("permlevel") == 1
	assert _field(doc, "registration_capacity").get("permlevel") == 1


# --------------------------------------------------------------------------- #
# SCOPED_DOCTYPES + MEMBER_ANCHORED_DOCTYPES registration (FLO-7 §6.2 / §6.3).
# --------------------------------------------------------------------------- #
def test_flock_event_registration_registered_in_scoped_doctypes():
	# §6.2: every group-level DocType registers so the central
	# permission_query_conditions hook narrows it.
	assert "Flock Event Registration" in perms.SCOPED_DOCTYPES


def test_flock_event_registration_is_member_anchored_on_registrant_column():
	# §6.3 edit #4: a registration is about a person, so the self-membership
	# branch predicates on the `registrant` column (not `member`). The dict
	# maps doctype → the exact member-column name.
	assert "Flock Event Registration" in perms.MEMBER_ANCHORED_DOCTYPES
	assert perms.MEMBER_ANCHORED_DOCTYPES["Flock Event Registration"] == "registrant"


def test_member_anchored_doctypes_map_is_dict_with_member_column():
	# The generalization (FLO-62) keeps Flock Group Member on `member` and adds
	# the registration on `registrant`. Every entry maps to a real column name.
	assert isinstance(perms.MEMBER_ANCHORED_DOCTYPES, dict)
	assert perms.MEMBER_ANCHORED_DOCTYPES["Flock Group Member"] == "member"


def test_group_scope_sql_emits_registrant_column_for_registration():
	# The SQL builder must use the mapped column (`registrant`), not a hard-
	# coded `member`, so the self-membership clause is valid SQL on the
	# registration table. (``escape=str`` is the unit-test passthrough — values
	# are unquoted; production wires ``frappe.db.escape`` which quotes them.)
	scope = perms.LeaderScope(
		member="M1",
		led_bounds=(perms.GroupBounds("G1", 2, 5),),
		joined_groups=(),
	)
	sql = perms.build_group_scope_sql(doctype="Flock Event Registration", scope=scope, escape=str)
	assert "`tabFlock Event Registration`.`registrant` = M1" in sql
	# And the registration still gets the subtree + NULL-group passthrough.
	assert "IN (SELECT name FROM `tabFlock Group`" in sql


def test_group_scope_sql_still_uses_member_for_group_member():
	# Regression guard: the dict change must not alter Flock Group Member's SQL.
	scope = perms.LeaderScope(member="M1", led_bounds=(), joined_groups=("G2",))
	sql = perms.build_group_scope_sql(doctype="Flock Group Member", scope=scope, escape=str)
	assert "`tabFlock Group Member`.`member` = M1" in sql


def test_group_scope_sql_suppresses_member_clause_for_gathering():
	# Flock Gathering has no member column → no self-membership clause (FLO-54).
	scope = perms.LeaderScope(member="M1", led_bounds=(perms.GroupBounds("G1", 2, 5),))
	sql = perms.build_group_scope_sql(doctype="Flock Gathering", scope=scope, escape=str)
	assert "`member` =" not in sql
	assert "`registrant` =" not in sql


# --------------------------------------------------------------------------- #
# v0_2 index patch contract (FLO-7 §3.5 / §5 #4).
# --------------------------------------------------------------------------- #
EXPECTED_REGISTRATION_INDEXES = {
	("Flock Event Registration", ("gathering", "registrant")),
}


def test_registration_index_contract_matches_patch():
	"""The v0_2 patch's UNIQUE indexes must target the registration DocType and
	reference real columns; the (doctype, columns) UNIQUE set must match the
	expected idempotency contract."""
	from flock_os.patches.v0_2.add_registration_indexes import INDEXES

	schema = _load_doctype("Flock Event Registration")
	field_names = {f["fieldname"] for f in schema["fields"]}

	unique_declared = set()
	for doctype, _index_name, columns_sql, unique in INDEXES:
		assert doctype == "Flock Event Registration", f"patch index targets {doctype!r}"
		columns = tuple(c.strip().strip("`") for c in columns_sql.strip("()").split(","))
		for col in columns:
			assert col in field_names, f"index column {col!r} is not a Flock Event Registration field"
		if unique:
			unique_declared.add((doctype, columns))

	assert unique_declared == EXPECTED_REGISTRATION_INDEXES


def test_registration_index_patch_declares_hot_path_nonunique_indexes():
	# §3.5: (gathering, registration_status) + (branch, registered_at) hot-path
	# indexes (non-unique) for capacity/waitlist + branch-scoped reads.
	from flock_os.patches.v0_2.add_registration_indexes import INDEXES

	# A non-unique index references registration_status (capacity/waitlist reads).
	assert any("registration_status" in columns and not unique for _dt, _name, columns, unique in INDEXES)
	# A non-unique index references registered_at (branch-scoped roll reads).
	assert any("registered_at" in columns and not unique for _dt, _name, columns, unique in INDEXES)


def test_registration_index_patch_registered_in_patches_txt():
	# The patch must run post_model_sync (after the registration table exists).
	patches_txt = (Path(__file__).resolve().parent.parent / "patches.txt").read_text()
	assert "flock_os.patches.v0_2.add_registration_indexes" in patches_txt


# --------------------------------------------------------------------------- #
# Capacity-race + insert-ordering contract (FLO-7 §5 #3 / §5 #4).
#
# The controller (Flock Event Registration) is coverage-omitted (runs under
# `bench run-tests`, not the project gate), so this source-level guard pins the
# two correctness invariants the architect review required:
#   1. The Registered/Waitlisted verdict is derived from the AUTHORITATIVE
#      locked count (SELECT ... FOR UPDATE), not an optimistic re-read that
#      over-admits beyond capacity on a losing race (§5 #3).
#   2. The counter is bumped AFTER the registration row lands (insert-first),
#      so a failed unique-constraint insert rolls back without moving the
#      counter — no phantom seat (§5 #4).
# A regression of either surfaces as a failing assertion here, without a bench.
# --------------------------------------------------------------------------- #
_CONTROLLER = (
	Path(__file__).resolve().parent.parent
	/ "flock_os"
	/ "doctype"
	/ "flock_event_registration"
	/ "flock_event_registration.py"
)


def test_capacity_verdict_uses_row_lock_not_optimistic_reread():
	# §5 #3: the verdict must come from SELECT ... FOR UPDATE so a losing
	# claimant lands Waitlisted instead of over-admitting. The old
	# `_increment_registered_count` (conditional UPDATE + re-read that could
	# never distinguish "I claimed it" from "someone else did") is gone.
	src = _CONTROLLER.read_text()
	assert "_increment_registered_count" not in src, "old racy helper must be removed"
	assert "FOR UPDATE" in src, "capacity verdict must lock the gathering row"
	assert "_authoritative_registration_status" in src


def test_counter_bumped_after_insert_not_before():
	# §5 #4: insert-first — the counter bump (+1) must follow the registration
	# insert, so a failed unique insert rolls back without a phantom seat.
	src = _CONTROLLER.read_text()
	register_body = src[src.index("def register_for_event") :]
	insert_idx = register_body.index("doc.insert(")
	bump_idx = register_body.index("_bump_registered_count(gathering, +1)")
	assert insert_idx < bump_idx, "counter must be bumped AFTER the row insert (insert-first, §5 #4)"
	# The bump is gated on Registered (a Waitlisted row never moves the counter).
	assert "REGISTRATION_REGISTERED" in register_body[bump_idx - 200 : bump_idx + 40]
	# The duplicate-race path rolls back a lost uniqueness race (no phantom seat).
	assert "DuplicateEntryError" in register_body
	assert "frappe.db.rollback()" in register_body
