"""15k-scale data-volume seeder for the local bench (FLO-365).

Materializes a realistic ~15,000-attendee event dataset across the Flock Branch +
Flock Group trees so the DB/application hot paths can be profiled at production
event volume on a local bench — the data-tier gap [FLO-365](/FLO/issues/FLO-365)
closes (distinct from the WS-tier work in [FLO-347](/FLO/issues/FLO-347)).

Dataset shape (configurable via :func:`execute` kwargs):

* **1 organization** — the site's singleton ``Flock Organization`` (reused, never
  a parallel org — same invariant as the smoke seeder).
* **N branches** (default 5) — ``scale-branch-<k>``, explicit PKs.
* **Groups** — a small nested tree per branch (root + 2 children) so the
  group-subtree permission / traversal paths have depth to walk.
* **~15,000 members** — bulk SQL insert (``db.bulk_insert``) with explicit
  ``scale-member-<i>`` names, distributed across branches. Bulk SQL bypasses the
  ORM/naming-series to keep the seed fast; members are structural fixtures, not
  the hot path under test.
* **Gatherings** — one routine "anchor" gathering per branch (the attendance
  hot path) + one ``One-time`` event on branch 1 (the registration hot path).
* **~15,000 attendance rows** — seeded **through the canonical
  :class:`flock_os.reporting.BulkAttendanceService`** (``FrappeBulkAttendanceGateway``)
  in 500-item batches. This IS the production write path; seeding through it
  exercises the real bulk-insert + idempotency + maintained-aggregate code while
  populating the summary rollup.
* **Registrations** — bulk SQL insert against the one-time event so the
  registration read/dashboard paths have volume without depending on an RQ worker.

Idempotent: every seeded row carries the ``SCALE_`` marker (``scale-*`` names /
a ``source = scale_seed`` tag). Re-runs delete the tagged rows first via
:func:`purge_scale_data` so the drill is repeatable on a clean bench — the FLO-365
acceptance bar. **Not** wired into ``patches.txt``; this is a dev/stress tool,
never a prod migration.

Run on the bench:

    bench --site <site> execute flock_os.utils.scale_seed.execute
    # or the wrapper: scripts/dev/seed-15k-scale.sh

The companion profiler is :mod:`flock_os.utils.scale_profile`; findings land in
``docs/operations/scale-15k-findings.md``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import frappe

from flock_os.reporting import (
	BULK_BATCH_SIZE,
	AttendanceItem,
	AttendanceScope,
	BulkAttendanceService,
	FrappeBulkAttendanceGateway,
)

# ---------------------------------------------------------------------------- #
# Constants — the marker that namespaces every seeded row so the drill is
# purgeable + idempotent. Pure constants (not magic strings) per AGENTS.md DRY.
# ---------------------------------------------------------------------------- #
SCALE_MARKER = "scale"
SCALE_BRANCH_PREFIX = "scale-branch"
SCALE_GROUP_NAME = "Scale Group"
SCALE_MEMBER_PREFIX = "scale-member"
SCALE_GATHERING_TITLE = "Scale Anchor Gathering"
SCALE_ONE_TIME_TITLE = "Scale One-Time Event"
SCALE_GATHERING_TYPE = "Scale Type"
SCALE_SOURCE_TAG = "scale_seed"

DEFAULT_BRANCH_COUNT = 5
"""Branches in the scale tree (default 5 → ~3k members/branch at 15k)."""

DEFAULT_TOTAL_MEMBERS = 15_000
"""Total members seeded across all branches (the FLO-1 15k-attendee bar)."""

DEFAULT_ATTENDANCE_PER_ANCHOR = 3_000
"""Attendance rows per anchor gathering. 5 anchors × 3k = 15k attendance rows."""


@dataclass
class ScaleSeedReport:
	"""Receipt returned by :func:`execute` — captured by the profiler / findings."""

	branch_count: int = 0
	group_count: int = 0
	member_count: int = 0
	gathering_count: int = 0
	attendance_inserted: int = 0
	registration_count: int = 0
	elapsed_seconds: float = 0.0
	branches: list[str] = field(default_factory=list)
	anchor_gatherings: list[str] = field(default_factory=list)
	one_time_gathering: str | None = None
	wps: float = 0.0
	"""Attendance write throughput (rows/s) through the canonical bulk service."""

	def as_dict(self) -> dict:
		return {
			"branch_count": self.branch_count,
			"group_count": self.group_count,
			"member_count": self.member_count,
			"gathering_count": self.gathering_count,
			"attendance_inserted": self.attendance_inserted,
			"registration_count": self.registration_count,
			"elapsed_seconds": round(self.elapsed_seconds, 3),
			"wps": round(self.wps, 1),
			"branches": self.branches,
			"anchor_gatherings": self.anchor_gatherings,
			"one_time_gathering": self.one_time_gathering,
		}


# ---------------------------------------------------------------------------- #
# Purge — remove every tagged row so re-runs are a clean, repeatable drill.
# Order respects FK dependencies (attendance/registration → gathering → member
# → group → branch). Summary rows are scoped by tagged branches.
# ---------------------------------------------------------------------------- #
def purge_scale_data() -> None:
	"""Delete every ``scale-*`` tagged row (idempotent re-run precondition).

	Order respects FK dependencies (attendance/registration → gathering → member
	→ group → branch). Summary rows are scoped by tagged branches. Each delete
	is guarded by :func:`_table_exists` so the drill is resilient to a partial
	bench (some Phase 2+ doctypes may not be migrated yet) — attendance, the
	headline data-tier concern, is always present.
	"""
	_delete_where("Flock Attendance Record", "branch", SCALE_BRANCH_PREFIX)
	_delete_where("Flock Event Registration", "branch", SCALE_BRANCH_PREFIX)
	_delete_where("Event Attendance Summary", "branch", SCALE_BRANCH_PREFIX)
	_delete_like("Flock Gathering", "title", f"{SCALE_GATHERING_TITLE}%")
	_delete_like("Flock Gathering", "title", f"{SCALE_ONE_TIME_TITLE}%")
	_delete_where("Flock Member", "name", SCALE_MEMBER_PREFIX)
	_delete_like("Flock Group Member", "group", f"{SCALE_BRANCH_PREFIX}-%")
	_delete_like("Flock Group", "name", f"{SCALE_BRANCH_PREFIX}-%")
	_delete_like("Flock Branch", "name", f"{SCALE_BRANCH_PREFIX}%")
	frappe.db.commit()


def _table_exists(doctype: str) -> bool:
	"""True iff the doctype's table is migrated on this bench."""
	return bool(
		frappe.db.exists("DocType", doctype)
		and frappe.db.sql(
			"SELECT 1 FROM information_schema.tables WHERE table_schema = DATABASE() AND table_name = %s",
			values=(f"tab{doctype}",),
		)
	)


def _delete_where(doctype: str, fieldname: str, prefix: str) -> None:
	if not _table_exists(doctype):
		return
	frappe.db.delete(doctype, filters={fieldname: ["like", f"{prefix}%"]})


def _delete_like(doctype: str, fieldname: str, pattern: str) -> None:
	if not _table_exists(doctype):
		return
	frappe.db.delete(doctype, filters={fieldname: ["like", pattern]})


# ---------------------------------------------------------------------------- #
# Structural seeders (ORM — small N, validates bindings).
# ---------------------------------------------------------------------------- #
def _resolve_organization() -> str:
	"""Reuse the site singleton (never a parallel scale org)."""
	existing = frappe.db.get_value("Flock Organization", {}, "name")
	if existing:
		return existing
	doc = frappe.get_doc({"doctype": "Flock Organization", "organization_name": "Flock OS"})
	doc.insert(ignore_permissions=True)
	return doc.name


def _seed_gathering_type(org: str) -> str:
	name = SCALE_GATHERING_TYPE
	if not frappe.db.exists("Flock Gathering Type", name):
		frappe.get_doc(
			{
				"doctype": "Flock Gathering Type",
				"gathering_type_name": name,
				"organization": org,
			}
		).insert(ignore_permissions=True)
	return name


def _seed_branches(org: str, count: int) -> list[str]:
	"""Create ``count`` branches; return their PKs."""
	branches = []
	for k in range(1, count + 1):
		name = f"{SCALE_BRANCH_PREFIX}-{k}"
		if not frappe.db.exists("Flock Branch", name):
			frappe.get_doc(
				{
					"doctype": "Flock Branch",
					"branch_name": name,
					"organization": org,
					"parent_branch": "",
				}
			).insert(ignore_permissions=True)
		branches.append(name)
	frappe.db.commit()
	return branches


def _seed_groups(org: str, branches: list[str]) -> list[str]:
	"""A root group + 2 nested children per branch (depth for subtree walks)."""
	groups = []
	for branch in branches:
		root_name = f"{branch}-{SCALE_GROUP_NAME}-Root"
		if not frappe.db.exists("Flock Group", root_name):
			frappe.get_doc(
				{
					"doctype": "Flock Group",
					"group_name": f"{SCALE_GROUP_NAME}-Root",
					"branch": branch,
					"organization": org,
					"parent_group": "",
					"is_group": 1,
				}
			).insert(ignore_permissions=True)
		groups.append(root_name)
		for child in ("Alpha", "Beta"):
			child_name = f"{branch}-{SCALE_GROUP_NAME}-{child}"
			if not frappe.db.exists("Flock Group", child_name):
				frappe.get_doc(
					{
						"doctype": "Flock Group",
						"group_name": f"{SCALE_GROUP_NAME}-{child}",
						"branch": branch,
						"organization": org,
						"parent_group": root_name,
					}
				).insert(ignore_permissions=True)
			groups.append(child_name)
	frappe.db.commit()
	return groups


def _seed_gatherings(org: str, branches: list[str], gtype: str, anchor_date: str) -> tuple[list[str], str]:
	"""One routine anchor per branch + one one-time event on branch 1."""
	anchors = []
	for branch in branches:
		root_group = f"{branch}-{SCALE_GROUP_NAME}-Root"
		title = f"{SCALE_GATHERING_TITLE} {branch}"
		existing = frappe.db.get_value("Flock Gathering", {"title": title})
		if not existing:
			doc = frappe.get_doc(
				{
					"doctype": "Flock Gathering",
					"organization": org,
					"branch": branch,
					"group": root_group,
					"gathering_type": gtype,
					"title": title,
					"starts_on": anchor_date,
					"status": "Scheduled",
					"event_category": "Routine",
				}
			)
			doc.insert(ignore_permissions=True)
			existing = doc.name
		anchors.append(existing)

	# One-time event on branch 1 — the registration hot path. Approved + open
	# window + Branch scope + high capacity so the registration/dashboard reads
	# have realistic volume.
	ot_title = f"{SCALE_ONE_TIME_TITLE} 1"
	ot_group = f"{branches[0]}-{SCALE_GROUP_NAME}-Root"
	ot_existing = frappe.db.get_value("Flock Gathering", {"title": ot_title})
	if not ot_existing:
		doc = frappe.get_doc(
			{
				"doctype": "Flock Gathering",
				"organization": org,
				"branch": branches[0],
				"group": ot_group,
				"gathering_type": gtype,
				"title": ot_title,
				"starts_on": anchor_date,
				"status": "Scheduled",
				"event_category": "One-time",
				"approval_status": "Approved",
				"registration_scope": "Branch",
				"registration_capacity": 20_000,
				"registration_opens_on": "2020-01-01 00:00:00",
				"registration_closes_on": "2099-12-31 23:59:59",
				"capacity": 20_000,
			}
		)
		doc.insert(ignore_permissions=True)
		ot_existing = doc.name
	frappe.db.commit()
	return anchors, ot_existing


# ---------------------------------------------------------------------------- #
# High-volume seeders — bulk SQL for members/registrations (structural fixtures,
# not the hot path). The attendance path uses the canonical bulk service.
# ---------------------------------------------------------------------------- #
def _seed_members_bulk(org: str, branches: list[str], total: int) -> int:
	"""Bulk-insert ``total`` members distributed evenly across branches.

	Uses ``db.bulk_insert`` with explicit ``scale-member-<i>`` names to bypass
	the naming series (members are fixtures, not the hot path). Chunks of 1000
	to keep statement size sane.
	"""
	fields = [
		"name",
		"first_name",
		"last_name",
		"full_name",
		"status",
		"branch",
		"organization",
		"is_active",
	]
	chunk = 1000
	inserted = 0
	per_branch = total // len(branches)
	values: list[list] = []
	for i in range(total):
		branch = branches[min(i // max(per_branch, 1), len(branches) - 1)]
		values.append(
			[
				f"{SCALE_MEMBER_PREFIX}-{i}",
				"Scale",
				f"Member{i}",
				f"Scale Member{i}",
				"Member",
				branch,
				org,
				1,
			]
		)
		if len(values) >= chunk:
			frappe.db.bulk_insert("Flock Member", fields=fields, values=values)
			frappe.db.commit()
			inserted += len(values)
			values = []
	if values:
		frappe.db.bulk_insert("Flock Member", fields=fields, values=values)
		frappe.db.commit()
		inserted += len(values)
	return inserted


def _seed_attendance_via_bulk_service(
	branches: list[str], anchors: list[str], per_anchor: int
) -> tuple[int, float]:
	"""Seed attendance through the canonical BulkAttendanceService (hot path).

	Returns (rows_inserted, elapsed_seconds). Each anchor gets ``per_anchor``
	rows split into BULK_BATCH_SIZE batches, submitted through the real service
	so the bulk-insert + idempotency + maintained-aggregate code runs at volume.
	"""
	service = BulkAttendanceService(FrappeBulkAttendanceGateway())
	inserted = 0
	start = time.perf_counter()
	for branch, gathering in zip(branches, anchors, strict=True):
		scope = AttendanceScope(branch=branch)
		for batch_idx in range(0, per_anchor, BULK_BATCH_SIZE):
			size = min(BULK_BATCH_SIZE, per_anchor - batch_idx)
			items = [
				AttendanceItem(
					event=gathering,
					attendee_ref=f"{SCALE_MEMBER_PREFIX}-{batch_idx + j}",
					branch=branch,
					client_req_id=f"scale:{gathering}:{batch_idx + j}",
				)
				for j in range(size)
			]
			outcome = service.submit(items, scope, batch_id=f"scale-{gathering}-{batch_idx}")
			inserted += outcome.inserted
	elapsed = time.perf_counter() - start
	return inserted, elapsed


def _seed_registrations_bulk(gathering: str, branch: str, group: str, org: str, total: int) -> int:
	"""Bulk-insert registrations for the one-time event (registration read volume).

	Direct SQL (not the RQ bulk path) so the seed doesn't depend on a worker.
	The registration hot path under test is the READ/dashboard path; the write
	path is exercised separately by the profiler. Skipped (returns 0) when the
	``Flock Event Registration`` table is not migrated on the bench.
	"""
	if not _table_exists("Flock Event Registration"):
		frappe.logger("flock_os.scale").info(
			"scale_seed: Flock Event Registration table absent — skipping registration seed"
		)
		return 0
	fields = [
		"name",
		"organization",
		"branch",
		"group",
		"gathering",
		"registrant",
		"registrant_name",
		"registration_status",
		"registered_at",
		"registered_via",
	]
	now = frappe.utils.now()
	values = [
		[
			f"scale-reg-{i}",
			org,
			branch,
			group,
			gathering,
			f"{SCALE_MEMBER_PREFIX}-{i}",
			f"Scale Member{i}",
			"Registered",
			now,
			"Bulk",
		]
		for i in range(total)
	]
	frappe.db.bulk_insert("Flock Event Registration", fields=fields, values=values)
	# Keep the gathering counter consistent with the seeded rows (the dashboard
	# reads this counter, never COUNT(*)).
	frappe.db.set_value("Flock Gathering", gathering, "registered_count", total)
	frappe.db.commit()
	return total


# ---------------------------------------------------------------------------- #
# Entry point — callable via ``bench execute flock_os.utils.scale_seed.execute``.
# ---------------------------------------------------------------------------- #
def execute(
	*,
	branch_count: int = DEFAULT_BRANCH_COUNT,
	total_members: int = DEFAULT_TOTAL_MEMBERS,
	attendance_per_anchor: int = DEFAULT_ATTENDANCE_PER_ANCHOR,
	registrations: int = DEFAULT_TOTAL_MEMBERS,
	purge: bool = True,
) -> dict:
	"""Seed the 15k-scale dataset. Returns the :class:`ScaleSeedReport` as dict.

	``purge=True`` (default) wipes tagged rows first so the drill is repeatable
	on a clean bench — the FLO-365 acceptance bar.
	"""
	start = time.perf_counter()
	if purge:
		purge_scale_data()

	org = _resolve_organization()
	gtype = _seed_gathering_type(org)
	branches = _seed_branches(org, branch_count)
	groups = _seed_groups(org, branches)
	anchors, one_time = _seed_gatherings(org, branches, gtype, "2026-06-21 10:00:00")
	member_count = _seed_members_bulk(org, branches, total_members)

	att_inserted, att_elapsed = _seed_attendance_via_bulk_service(branches, anchors, attendance_per_anchor)

	ot_group = f"{branches[0]}-{SCALE_GROUP_NAME}-Root"
	reg_count = _seed_registrations_bulk(one_time, branches[0], ot_group, org, registrations)

	elapsed = time.perf_counter() - start
	wps = att_inserted / att_elapsed if att_elapsed > 0 else 0.0
	report = ScaleSeedReport(
		branch_count=len(branches),
		group_count=len(groups),
		member_count=member_count,
		gathering_count=len(anchors) + 1,
		attendance_inserted=att_inserted,
		registration_count=reg_count,
		elapsed_seconds=elapsed,
		branches=branches,
		anchor_gatherings=anchors,
		one_time_gathering=one_time,
		wps=wps,
	)
	frappe.logger("flock_os.scale").info(f"scale_seed: {report.as_dict()}")
	return report.as_dict()
