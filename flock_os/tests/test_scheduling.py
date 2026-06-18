"""
Project-level tests for the P2.4 announcement scheduling + scoped-publish
service (FLO-55 / FLO-94 recovery, FLO-8 §2/§3/§6, ADR-0001 §4/§6).

These run under plain ``pytest`` (no Frappe site / Redis / bench), mirroring the
SQL-light hexagonal pattern from ``test_notifications.py`` /
``test_permissions.py``. They pin the FLO-55 / FLO-94 Definition of Done:

* **Scope contract** — ``validate_announcement_scope`` enforces the tenant floor,
  branch-in-org membership, the group-branch-binding rule (ADR §4), and the
  lifecycle invariants (status set, Scheduled-requires-scheduled_at).
* **Audience boundary, no cross-branch leakage** — ``resolve_audience_branches``
  returns the publisher branch + its subtree (reusing
  ``compute_branch_subtree``); sibling branches are never reached.
* **Lifecycle** — ``validate_status_transition`` permits only forward moves.
* **Permission wiring** — ``Flock Announcement`` is registered in
  ``SCOPED_DOCTYPES`` so the group-axis hook + NULL-group passthrough apply.
* **Reuse (DRY)** — the audience resolver delegates to
  ``flock_os.permissions.compute_branch_subtree`` (no parallel traversal).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from flock_os import permissions as perms
from flock_os import scheduling
from flock_os.scheduling import (
	ANNOUNCEMENT_TRANSITIONS,
	STATUS_ARCHIVED,
	STATUS_DRAFT,
	STATUS_PUBLISHED,
	STATUS_PUBLISHING,
	STATUS_SCHEDULED,
	FlockSchedulingError,
	SchedulingGateway,
	preview_audience,
	resolve_audience_branches,
	validate_announcement_scope,
	validate_status_transition,
)

ORG = "ORG"

# --------------------------------------------------------------------------- #
# In-memory world — a two-branch org tree (ADR §4). Audience resolution for
# `North` must reach {North, North-A} and NEVER `South` (no leakage).
#
#   HQ ─── North ─── North-A     (group G-North-A bound to branch North-A)
#         South
# --------------------------------------------------------------------------- #


class RecordingSchedulingGateway:
	"""In-memory scheduling gateway over a known two-tree world."""

	BRANCH_PARENT = {"HQ": None, "North": "HQ", "North-A": "North", "South": "HQ"}
	BRANCH_CHILDREN = {
		"HQ": ["North", "South"],
		"North": ["North-A"],
		"North-A": [],
		"South": [],
	}
	# group -> branch binding (ADR §4 group-branch-binding).
	GROUP_BRANCH = {"G-North-A": "North-A", "G-South": "South"}

	def branch_exists(self, branch: str, organization: str) -> bool:
		return branch in self.BRANCH_PARENT and organization == ORG

	def group_exists(self, group: str) -> bool:
		return group in self.GROUP_BRANCH

	def group_branch(self, group: str) -> str | None:
		return self.GROUP_BRANCH.get(group)

	def branch_parent_of(self):
		return dict(self.BRANCH_PARENT)

	def branch_children_of(self):
		return {k: list(v) for k, v in self.BRANCH_CHILDREN.items()}


@pytest.fixture
def gw():
	return RecordingSchedulingGateway()


@pytest.fixture(autouse=True)
def _restore_gateway():
	"""Ensure a test-installed gateway never leaks into other modules."""
	original = scheduling._gateway
	yield
	scheduling._gateway = original


def _ann(**kwargs) -> SimpleNamespace:
	"""A minimal announcement object the validator reads via duck typing."""
	base = {
		"organization": ORG,
		"branch": "North",
		"group": None,
		"status": STATUS_DRAFT,
		"scheduled_at": None,
		"subject": "Hello",
		"audience_role": "Leaders Only",
	}
	base.update(kwargs)
	return SimpleNamespace(**base)


# --------------------------------------------------------------------------- #
# validate_announcement_scope — the scope contract (FLO-8 §3 / ADR §4/§6.1)
# --------------------------------------------------------------------------- #


def test_valid_branch_scoped_announcement_passes(gw):
	# A branch-subtree announcement (no group) is the common case.
	validate_announcement_scope(_ann(branch="North"), gw)  # no raise


def test_valid_group_scoped_announcement_passes(gw):
	# A group anchor whose branch matches the announcement's branch is valid.
	validate_announcement_scope(_ann(branch="North-A", group="G-North-A"), gw)


def test_missing_organization_raises(gw):
	with pytest.raises(FlockSchedulingError, match="organization"):
		validate_announcement_scope(_ann(organization=""), gw)


def test_missing_branch_raises(gw):
	with pytest.raises(FlockSchedulingError, match="branch"):
		validate_announcement_scope(_ann(branch=""), gw)


def test_branch_not_in_organization_raises(gw):
	# Cross-tenant guard: a branch rooted in another org is rejected.
	with pytest.raises(FlockSchedulingError, match="not a member of organization"):
		validate_announcement_scope(_ann(branch="North", organization="OTHER"), gw)


def test_unknown_group_raises(gw):
	with pytest.raises(FlockSchedulingError, match="does not exist"):
		validate_announcement_scope(_ann(branch="North", group="G-Ghost"), gw)


def test_group_branch_binding_mismatch_raises(gw):
	# ADR §4: the announcement's branch must equal the group's own branch.
	# G-South is bound to South; an announcement scoped to North may not target it.
	with pytest.raises(FlockSchedulingError, match="group-branch-binding"):
		validate_announcement_scope(_ann(branch="North", group="G-South"), gw)


def test_unknown_status_raises(gw):
	with pytest.raises(FlockSchedulingError, match="Unknown announcement status"):
		validate_announcement_scope(_ann(status="Wat"), gw)


def test_scheduled_without_send_at_raises(gw):
	with pytest.raises(FlockSchedulingError, match="scheduled_at"):
		validate_announcement_scope(_ann(status=STATUS_SCHEDULED, scheduled_at=None), gw)


def test_scheduled_with_send_at_passes(gw):
	validate_announcement_scope(_ann(status=STATUS_SCHEDULED, scheduled_at="2026-07-01 10:00:00"), gw)


# --------------------------------------------------------------------------- #
# resolve_audience_branches — the audience boundary (FLO-8 §7, DRY reuse)
# --------------------------------------------------------------------------- #


def test_audience_is_branch_plus_subtree(gw):
	# North + North-A — the publisher's branch subtree.
	branches = resolve_audience_branches("North", gateway=gw)
	assert branches == ("North", "North-A")


def test_audience_excludes_sibling_branches(gw):
	# FLO-57 DoD "no cross-branch leakage": South is a sibling, never a recipient.
	branches = resolve_audience_branches("North", gateway=gw)
	assert "South" not in branches
	assert "HQ" not in branches  # the parent is not a recipient either


def test_audience_root_covers_whole_tree(gw):
	branches = resolve_audience_branches("HQ", gateway=gw)
	assert set(branches) == {"HQ", "North", "North-A", "South"}


def test_resolve_audience_reuses_compute_branch_subtree(gw, monkeypatch):
	# DRY pin: the resolver delegates to permissions.compute_branch_subtree
	# (no parallel traversal). If the delegation breaks, this test fails.
	original = perms.compute_branch_subtree
	calls: list[str] = []

	def spy(branch, *, parent_of, children_of):
		calls.append(branch)
		return original(branch, parent_of=parent_of, children_of=children_of)

	# Patch the name the scheduling module bound at import time (scheduling.perms).
	monkeypatch.setattr(scheduling.perms, "compute_branch_subtree", spy)
	resolve_audience_branches("North", gateway=gw)
	assert calls == ["North"]


def test_resolve_audience_empty_branch_raises(gw):
	with pytest.raises(FlockSchedulingError, match="branch is required"):
		resolve_audience_branches("", gateway=gw)


def test_scheduling_gateway_protocol_is_satisfied(gw):
	# The recording gateway conforms to the hexagonal port (runtime check).
	assert isinstance(gw, SchedulingGateway)


# --------------------------------------------------------------------------- #
# validate_status_transition — lifecycle forward moves (FLO-8 §3)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
	"current,target",
	[
		(STATUS_DRAFT, STATUS_SCHEDULED),
		(STATUS_DRAFT, STATUS_PUBLISHED),
		(STATUS_SCHEDULED, STATUS_PUBLISHED),
		(STATUS_PUBLISHING, STATUS_PUBLISHED),
		(STATUS_PUBLISHED, STATUS_ARCHIVED),
	],
)
def test_legal_transitions_pass(current, target):
	validate_status_transition(current, target)  # no raise


@pytest.mark.parametrize(
	"current,target",
	[
		(STATUS_ARCHIVED, STATUS_DRAFT),  # terminal
		(STATUS_PUBLISHED, STATUS_DRAFT),  # no backward
		(STATUS_DRAFT, STATUS_ARCHIVED),  # skip publish
	],
)
def test_illegal_transitions_raise(current, target):
	with pytest.raises(FlockSchedulingError, match="Illegal announcement transition"):
		validate_status_transition(current, target)


def test_transitions_table_covers_all_statuses():
	# The lifecycle table must register every status (no orphan state).
	assert set(ANNOUNCEMENT_TRANSITIONS) == set(scheduling.ANNOUNCEMENT_STATUSES)


# --------------------------------------------------------------------------- #
# preview_audience controller — the compose-UI surface (FLO-8 §8)
# --------------------------------------------------------------------------- #


def test_preview_audience_returns_subtree_and_count(gw):
	scheduling.install_gateway(gw)
	out = preview_audience(organization=ORG, branch="North")
	assert out["branches"] == ["North", "North-A"]
	assert out["branch_count"] == 2


def test_preview_audience_validates_scope_first(gw):
	# An invalid scope is rejected before any audience is resolved.
	scheduling.install_gateway(gw)
	with pytest.raises(FlockSchedulingError, match="not a member of organization"):
		preview_audience(organization="OTHER", branch="North")


def test_preview_audience_group_scope(gw):
	scheduling.install_gateway(gw)
	out = preview_audience(organization=ORG, branch="North-A", group="G-North-A")
	# A group-scoped announcement still resolves the branch subtree.
	assert "North-A" in out["branches"]


# --------------------------------------------------------------------------- #
# Permission wiring (FLO-8 §6.2) — Flock Announcement is group-axis scoped
# --------------------------------------------------------------------------- #


def test_flock_announcement_registered_in_scoped_doctypes():
	# ADR §6.5: the single list both the group-axis hook + audit gate consult.
	# FLO-55 shipped this entry; the recovery re-confirms the wiring survives.
	assert "Flock Announcement" in perms.SCOPED_DOCTYPES


def test_group_axis_hook_handles_null_group_passthrough():
	# FLO-8 §6.2: branch-subtree announcements (group IS NULL) must fall through
	# to the native branch axis — the hook emits the NULL-group passthrough.
	# Pinned by the build_group_scope_sql contract for nullable-group DocTypes.
	from collections import namedtuple

	from flock_os.permissions import build_group_scope_sql

	Scope = namedtuple("Scope", "member led_bounds joined_groups")
	scope = Scope(member="M1", led_bounds=(), joined_groups=("G1",))
	sql = build_group_scope_sql(doctype="Flock Announcement", scope=scope, escape=str)
	assert "`group` IS NULL" in sql


# --------------------------------------------------------------------------- #
# Import-clean + module surface (DoD: import-clean under plain pytest)
# --------------------------------------------------------------------------- #


def test_scheduling_module_imports_without_frappe():
	# DoD: flock_os.scheduling import-clean under plain pytest. The whitelist
	# controller entry points exist and the pure functions are callable.
	import flock_os.scheduling as sched  # noqa: F401 (re-import is the test)

	assert callable(sched.validate_announcement_scope)
	assert callable(sched.resolve_audience_branches)
	assert callable(sched.publish_announcement)
	assert callable(sched.preview_audience)
