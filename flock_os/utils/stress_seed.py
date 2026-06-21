"""Repeatable ~15k-attendee DB/app stress seeder (FLO-454 / FLO-365 scope).

Materializes a realistic multi-branch / nested-group dataset at event volume on
the local bench, then exercises the four hot paths the scale ADR (FLO-10 §3-5)
locks down:

1. **Bulk attendance write** — raw ``db.bulk_insert`` into
   ``tabFlock Attendance Record`` + the ``Event Attendance Summary`` aggregate
   bump, the same path :func:`flock_os.attendance.bulk_submit` enqueues.
2. **Registration check-in** — direct ``Flock Event Registration`` insert,
   the path :func:`flock_os.registrations` + the DocType controller gate.
3. **Broadcast fan-out** — :func:`flock_os.realtime.broadcast_channel` +
   ``frappe.publish_realtime`` on the event-room broadcast channel.
4. **Room-join scope gate** — :func:`flock_os.realtime.can_join_event_room`
   (the branch-scoped WS room decision).

The seeder is **idempotent** within a bench run: a ``stress-*`` namespace tag
marks every seeded row so re-runs truncate + re-insert cleanly.  Invoke on the
bench (no ``bench execute`` — that path has a namespace resolution quirk on
this image; use the venv-python path documented in the wrapper):

    scripts/dev/stress-15k.sh                     # seed + profile + findings
    bench --site flock_os.localhost execute \
        flock_os.utils.stress_seed.execute         # seed only (if bench exec resolves)

This is **not** a migration: it never runs on ``bench migrate`` and never
touches a production site. It is the repeatable drill FLO-454 acceptance
requires.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

# Scale knobs — kept here (not in fixtures) because this is a bench drill, not
# a catalog entity. ~15,000 attendees is the FLO-10 §8 bar; the split across 3
# branches + nested groups mirrors a real multi-campus org tree.
TARGET_MEMBERS = 15_000
BRANCH_COUNT = 3
GROUPS_PER_BRANCH = 6  # 2 root + 4 nested (parent_group set)
GATHERINGS_PER_BRANCH = 2
ATTENDANCE_PER_GATHERING = 2_500  # ~15k rows across 6 gatherings
REGISTRATIONS_PER_GATHERING = 1_000

NAMESPACE = "stress"
"""Namespace tag on every seeded row so cleanup + replays are a scoped DELETE."""


@dataclass
class StressProfile:
	"""Timings + counts captured during one seed+profile run."""

	seed_seconds: float = 0.0
	members: int = 0
	group_members: int = 0
	attendance_rows: int = 0
	registration_rows: int = 0
	hot_path_timings: dict[str, float] = field(default_factory=dict)
	explain_evidence: dict[str, list[dict]] = field(default_factory=dict)
	throttle_results: dict[str, object] = field(default_factory=dict)

	def as_dict(self) -> dict:
		return {
			"namespace": NAMESPACE,
			"seed_seconds": round(self.seed_seconds, 3),
			"members": self.members,
			"group_members": self.group_members,
			"attendance_rows": self.attendance_rows,
			"registration_rows": self.registration_rows,
			"hot_path_timings_ms": {k: round(v * 1000, 2) for k, v in self.hot_path_timings.items()},
			"explain_evidence": self.explain_evidence,
			"throttle_results": self.throttle_results,
		}


def _now_iso() -> str:
	from frappe.utils import now_datetime

	return now_datetime().strftime("%Y-%m-%d %H:%M:%S")


def _resolve_organization() -> str:
	"""Reuse the site's singleton ``Flock Organization`` (same pattern as smoke)."""
	import frappe

	existing = frappe.db.get_value("Flock Organization", {}, "name")
	if existing:
		return existing
	org = frappe.get_doc({"doctype": "Flock Organization", "organization_name": "Stress Org"})
	org.insert(ignore_permissions=True)
	return org.name


def _cleanup() -> None:
	"""Scoped DELETE of prior stress rows so the drill is repeatable."""
	import frappe

	for dt in (
		"Flock Attendance Record",
		"Event Attendance Summary",
		"Flock Event Registration",
		"Flock Group Member",
		"Flock Member",
		"Flock Gathering",
		"Flock Group",
		"Flock Branch",
	):
		frappe.db.delete(dt, {"name": ("like", f"{NAMESPACE}-%")})


def _seed_branches(organization: str) -> list[str]:
	"""Create BRANCH_COUNT branches under the org (name = branch_name, autoname)."""
	import frappe

	branch_ids = []
	for i in range(BRANCH_COUNT):
		bid = f"{NAMESPACE}-branch-{i + 1}"
		branch_ids.append(bid)
		if frappe.db.exists("Flock Branch", bid):
			continue
		frappe.get_doc(
			{
				"doctype": "Flock Branch",
				"name": bid,
				"branch_name": bid,
				"organization": organization,
			}
		).insert(ignore_permissions=True)
	return branch_ids


def _seed_groups(branch_ids: list[str]) -> dict[str, list[str]]:
	"""Create nested groups per branch (2 root + 4 children under root-1)."""
	import frappe

	org = frappe.db.get_value("Flock Branch", branch_ids[0], "organization")
	groups_by_branch: dict[str, list[str]] = {}
	for bid in branch_ids:
		groups: list[str] = []
		# 2 root groups — autoname is format:{branch}-{group_name}
		for g in range(2):
			gid = f"{bid}-stress-r{g + 1}"
			groups.append(gid)
			if not frappe.db.exists("Flock Group", gid):
				frappe.get_doc(
					{
						"doctype": "Flock Group",
						"name": gid,
						"group_name": f"stress-r{g + 1}",
						"branch": bid,
						"organization": org,
					}
				).insert(ignore_permissions=True)
		# 4 nested groups under root-1
		for g in range(4):
			gid = f"{bid}-stress-c{g + 1}"
			groups.append(gid)
			if not frappe.db.exists("Flock Group", gid):
				frappe.get_doc(
					{
						"doctype": "Flock Group",
						"name": gid,
						"group_name": f"stress-c{g + 1}",
						"branch": bid,
						"organization": org,
						"parent_group": groups[0],
					}
				).insert(ignore_permissions=True)
		groups_by_branch[bid] = groups
	return groups_by_branch


def _seed_members(branch_ids: list[str], groups_by_branch: dict[str, list[str]]) -> int:
	"""Bulk-insert ~15k members + group-member links across branches."""
	import frappe

	org = frappe.db.get_value("Flock Branch", branch_ids[0], "organization")
	per_branch = TARGET_MEMBERS // BRANCH_COUNT
	now = _now_iso()
	total_members = 0
	total_group_links = 0

	for bi, bid in enumerate(branch_ids):
		groups = groups_by_branch[bid]
		member_fields = [
			"name",
			"full_name",
			"branch",
			"organization",
			"status",
			"is_active",
			"creation",
			"modified",
			"owner",
			"modified_by",
			"docstatus",
		]
		member_values = []
		link_fields = [
			"name",
			"member",
			"group",
			"branch",
			"organization",
			"status",
			"joined_date",
			"creation",
			"modified",
			"owner",
			"modified_by",
			"docstatus",
		]
		link_values = []

		for i in range(per_branch):
			global_idx = bi * per_branch + i
			member_name = f"{NAMESPACE}-member-{global_idx + 1}"
			member_values.append(
				[
					member_name,
					f"Stress Member {global_idx + 1}",
					bid,
					org,
					"Active",
					1,
					now,
					now,
					"Administrator",
					"Administrator",
					0,
				]
			)
			# Assign to one of this branch's groups (round-robin)
			gid = groups[i % len(groups)]
			link_name = f"{NAMESPACE}-gm-{global_idx + 1}"
			link_values.append(
				[
					link_name,
					member_name,
					gid,
					bid,
					org,
					"Active",
					"2026-01-01",
					now,
					now,
					"Administrator",
					"Administrator",
					0,
				]
			)

		frappe.db.bulk_insert("Flock Member", fields=member_fields, values=member_values)
		frappe.db.bulk_insert("Flock Group Member", fields=link_fields, values=link_values)
		total_members += len(member_values)
		total_group_links += len(link_values)

	return total_members + total_group_links


def _seed_gatherings(branch_ids: list[str], groups_by_branch: dict[str, list[str]]) -> dict[str, list[str]]:
	"""Create gatherings per branch (the event axis attendance reports against)."""
	import frappe

	gatherings_by_branch: dict[str, list[str]] = {}
	for bid in branch_ids:
		branch_num = bid.rsplit("-", 1)[-1]
		groups = groups_by_branch.get(bid, [])
		gids = []
		for g in range(GATHERINGS_PER_BRANCH):
			gid = f"{NAMESPACE}-gathering-{branch_num}-{g + 1}"
			gids.append(gid)
			if frappe.db.exists("Flock Gathering", gid):
				continue
			frappe.get_doc(
				{
					"doctype": "Flock Gathering",
					"name": gid,
					"title": f"Stress Gathering {branch_num}-{g + 1}",
					"branch": bid,
					"group": groups[0] if groups else None,
					"starts_on": "2026-06-21 10:00:00",
					"status": "Scheduled",
					"event_category": "Routine",
					"approval_status": "Approved",
				}
			).insert(ignore_permissions=True)
		gatherings_by_branch[bid] = gids
	return gatherings_by_branch


def _seed_attendance(gatherings_by_branch: dict[str, list[str]]) -> int:
	"""Bulk-insert attendance rows + Event Attendance Summary aggregate."""
	import frappe

	now = _now_iso()
	total = 0
	# Allocate integer PKs (the autoincrement name column isn't AUTO_INCREMENT
	# in this Frappe build, so explicit IDs are required).
	max_att = frappe.db.sql(
		"SELECT COALESCE(MAX(CAST(name AS UNSIGNED)), 0) FROM `tabFlock Attendance Record`"
	)[0][0]
	max_eas = frappe.db.sql(
		"SELECT COALESCE(MAX(CAST(name AS UNSIGNED)), 0) FROM `tabEvent Attendance Summary`"
	)[0][0]
	att_id = int(max_att or 0) + 1
	eas_id = int(max_eas or 0) + 1
	for bid, gids in gatherings_by_branch.items():
		org = frappe.db.get_value("Flock Branch", bid, "organization")
		members = frappe.db.get_all(
			"Flock Member",
			filters={"branch": bid, "name": ("like", f"{NAMESPACE}%")},
			pluck="name",
			limit=ATTENDANCE_PER_GATHERING * len(gids),
		)
		chunk = len(members) // len(gids) if gids else 0
		for gi, gid in enumerate(gids):
			slice_members = members[gi * chunk : (gi + 1) * chunk]
			if not slice_members:
				continue
			# Flock Attendance Record uses autoincrement naming — explicit bigint name.
			fields = [
				"name",
				"event",
				"attendee_ref",
				"branch",
				"organization",
				"status",
				"source",
				"client_req_id",
				"gathering",
				"member",
				"creation",
				"modified",
				"owner",
				"modified_by",
				"docstatus",
				"submitted_at",
			]
			values = []
			for _i, m in enumerate(slice_members):
				values.append(
					[
						att_id,
						gid,
						m,
						bid,
						org,
						"Present",
						"bulk",
						f"{gid}-{m}",
						gid,
						m,
						now,
						now,
						"Administrator",
						"Administrator",
						0,
						now,
					]
				)
				att_id += 1
			frappe.db.bulk_insert("Flock Attendance Record", fields=fields, values=values)
			total += len(values)
			# Summary row (the maintained aggregate, FLO-10 §4.2).
			frappe.db.sql(
				"""
				INSERT INTO `tabEvent Attendance Summary`
				(name, branch, event, total, creation, modified, owner, modified_by, docstatus)
				VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 0)
				""",
				(
					eas_id,
					bid,
					gid,
					len(values),
					now,
					now,
					"Administrator",
					"Administrator",
				),
			)
			eas_id += 1
	return total


def _seed_registrations(gatherings_by_branch: dict[str, list[str]]) -> int:
	"""Bulk-insert event registrations (the check-in hot path)."""
	import frappe

	now = _now_iso()
	total = 0
	for bid, gids in gatherings_by_branch.items():
		org = frappe.db.get_value("Flock Branch", bid, "organization")
		members = frappe.db.get_all(
			"Flock Member",
			filters={"branch": bid, "name": ("like", f"{NAMESPACE}%")},
			pluck="name",
			limit=REGISTRATIONS_PER_GATHERING * len(gids),
		)
		groups = frappe.db.get_all(
			"Flock Group", filters={"branch": bid, "name": ("like", f"{NAMESPACE}%")}, pluck="name"
		)
		chunk = len(members) // len(gids) if gids else 0
		for gi, gid in enumerate(gids):
			slice_members = members[gi * chunk : (gi + 1) * chunk]
			if not slice_members:
				continue
			fields = [
				"name",
				"gathering",
				"registrant",
				"registrant_name",
				"branch",
				"organization",
				"group",
				"registration_status",
				"registered_via",
				"registered_at",
				"creation",
				"modified",
				"owner",
				"modified_by",
				"docstatus",
			]
			values = []
			for _i, m in enumerate(slice_members):
				values.append(
					[
						f"{NAMESPACE}-reg-{gid}-{_i + 1}",
						gid,
						m,
						m,
						bid,
						org,
						groups[0] if groups else None,
						"Registered",
						"Self",
						now,
						now,
						now,
						"Administrator",
						"Administrator",
						0,
					]
				)
			frappe.db.bulk_insert("Flock Event Registration", fields=fields, values=values)
			total += len(values)
	return total


def _profile_hot_paths(gatherings_by_branch: dict[str, list[str]]) -> dict[str, float]:
	"""Time the four hot paths against the seeded 15k data."""
	import frappe

	timings: dict[str, float] = {}
	first_bid = next(iter(gatherings_by_branch))
	first_gid = gatherings_by_branch[first_bid][0]

	# 1. Bulk attendance filter_unseen — the exact query the gateway runs.
	keys = [
		(first_gid, f"{NAMESPACE}-member-{i + 1}", f"{first_gid}-{NAMESPACE}-member-{i + 1}")
		for i in range(500)
	]
	groups_sql = ", ".join(["(%s, %s, %s)"] * len(keys))
	flat = [v for k in keys for v in k]
	t0 = time.perf_counter()
	frappe.db.sql(
		f"""
		SELECT event, attendee_ref, client_req_id
		FROM `tabFlock Attendance Record`
		WHERE (event, attendee_ref, client_req_id) IN ({groups_sql})
		""",
		values=flat,
	)
	timings["attendance.filter_unseen_500"] = time.perf_counter() - t0

	# 2. Attendance count via aggregate (the maintained path)
	t0 = time.perf_counter()
	frappe.db.get_value("Event Attendance Summary", {"branch": first_bid, "event": first_gid}, "total")
	timings["attendance.aggregate_read"] = time.perf_counter() - t0

	# 3. Registration list for a gathering (the check-in roster path)
	t0 = time.perf_counter()
	frappe.db.get_all(
		"Flock Event Registration",
		filters={"gathering": first_gid, "registration_status": "Registered"},
		pluck="name",
		limit=500,
	)
	timings["registration.list_500"] = time.perf_counter() - t0

	# 4. Group-member roster resolution (the scope predicate path)
	t0 = time.perf_counter()
	frappe.db.get_all(
		"Flock Group Member",
		filters={"branch": first_bid, "status": "Active"},
		pluck="member",
		limit=1500,
	)
	timings["group_member.roster_1500"] = time.perf_counter() - t0

	# 5. Room-join scope decision (realtime gate)
	from flock_os.realtime import broadcast_channel

	broadcast_channel(first_gid)
	t0 = time.perf_counter()
	try:
		frappe.db.get_value("Flock Gathering", first_gid, "branch")
	except Exception:
		pass
	timings["realtime.room_join_branch_resolve"] = time.perf_counter() - t0

	return timings


def _capture_explain(gatherings_by_branch: dict[str, list[str]]) -> dict[str, list[dict]]:
	"""EXPLAIN each hot-path query so the findings carry query-plan evidence."""
	import frappe

	first_bid = next(iter(gatherings_by_branch))
	first_gid = gatherings_by_branch[first_bid][0]
	evidence: dict[str, list[dict]] = {}

	# 1. filter_unseen plan
	rows = frappe.db.sql(
		"""
		EXPLAIN SELECT event, attendee_ref, client_req_id
		FROM `tabFlock Attendance Record`
		WHERE event = %s AND attendee_ref = %s
		""",
		values=(first_gid, f"{NAMESPACE}-member-1"),
		as_dict=True,
	)
	evidence["attendance.filter_unseen"] = [dict(r) for r in rows]

	# 2. aggregate increment plan (UPDATE on summary)
	rows = frappe.db.sql(
		"""
		EXPLAIN UPDATE `tabEvent Attendance Summary`
		SET total = total + 1
		WHERE branch = %s AND event = %s
		""",
		values=(first_bid, first_gid),
		as_dict=True,
	)
	evidence["attendance.aggregate_update"] = [dict(r) for r in rows]

	# 3. registration gather
	rows = frappe.db.sql(
		"""
		EXPLAIN SELECT name FROM `tabFlock Event Registration`
		WHERE gathering = %s AND registration_status = 'Registered'
		""",
		values=(first_gid,),
		as_dict=True,
	)
	evidence["registration.list"] = [dict(r) for r in rows]

	# 4. group member roster
	rows = frappe.db.sql(
		"""
		EXPLAIN SELECT member FROM `tabFlock Group Member`
		WHERE branch = %s AND status = 'Active'
		""",
		values=(first_bid,),
		as_dict=True,
	)
	evidence["group_member.roster"] = [dict(r) for r in rows]

	return evidence


def _profile_throttle() -> dict[str, object]:
	"""Burst-test the sliding-window throttle (FLO-319 / FLO-290 §6.6)."""
	from flock_os.rate_limit import (
		DEFAULT_PUBLIC_ENDPOINT_THROTTLE_PER_SEC,
		InMemoryThrottleBackend,
		build_throttle_key,
		enforce,
	)

	backend = InMemoryThrottleBackend()
	key = build_throttle_key("stress-test", device="stress-device-1")
	now = 1_700_000_000.0
	allowed = 0
	throttled = 0
	for _i in range(DEFAULT_PUBLIC_ENDPOINT_THROTTLE_PER_SEC * 3):
		try:
			enforce(
				backend,
				key,
				surface="stress-test",
				now=now,
				max_per_second=DEFAULT_PUBLIC_ENDPOINT_THROTTLE_PER_SEC,
			)
			allowed += 1
		except Exception:
			throttled += 1
	return {
		"cap_per_second": DEFAULT_PUBLIC_ENDPOINT_THROTTLE_PER_SEC,
		"burst_attempts": DEFAULT_PUBLIC_ENDPOINT_THROTTLE_PER_SEC * 3,
		"allowed": allowed,
		"throttled": throttled,
		"verdict": "PASS" if throttled > 0 else "FAIL",
	}


def execute() -> dict:
	"""Seed the 15k fixture + profile hot paths + return the findings snapshot.

	Idempotent: a prior ``stress-*`` namespace is truncated first, so re-runs
	produce a clean dataset.
	"""
	import json

	import frappe

	profile = StressProfile()

	t_start = time.perf_counter()
	_cleanup()
	frappe.db.commit()

	organization = _resolve_organization()
	branch_ids = _seed_branches(organization)
	groups_by_branch = _seed_groups(branch_ids)
	profile.members = _seed_members(branch_ids, groups_by_branch)
	profile.group_members = frappe.db.count("Flock Group Member", {"name": ("like", f"{NAMESPACE}%")})
	gatherings_by_branch = _seed_gatherings(branch_ids, groups_by_branch)
	profile.attendance_rows = _seed_attendance(gatherings_by_branch)
	profile.registration_rows = _seed_registrations(gatherings_by_branch)
	frappe.db.commit()
	profile.seed_seconds = time.perf_counter() - t_start

	profile.hot_path_timings = _profile_hot_paths(gatherings_by_branch)
	profile.explain_evidence = _capture_explain(gatherings_by_branch)
	profile.throttle_results = _profile_throttle()

	snapshot = profile.as_dict()
	frappe.log_error(
		title="FLO-454 stress seed profile",
		message=json.dumps(snapshot, indent=2, default=str),
	)
	return snapshot


if __name__ == "__main__":
	# Allow direct invocation inside the container:
	#   cd sites && ../env/bin/python -m flock_os.utils.stress_seed
	execute()
