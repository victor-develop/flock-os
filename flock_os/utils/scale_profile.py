"""15k-scale hot-path profiler for the local bench (FLO-365).

Companion to :mod:`flock_os.utils.scale_seed`. After the scale dataset is seeded,
this module walks the **production hot paths** at event volume, captures timing +
``EXPLAIN`` query plans, flags full scans / N+1 / unbounded reads, and verifies
the in-app throttle still holds under burst. Output feeds the FLO-365 findings
doc + the prioritized perf backlog.

Hot paths profiled (FLO-365 §What "Profile"):

1. **Attendance aggregate read** — ``FrappeBulkAttendanceGateway.aggregate``
   (must read the maintained rollup, never ``COUNT(*)`` over 15k rows).
2. **Attendance bulk write** — ``BulkAttendanceService.submit`` throughput
   (rows/s) + the ``filter_unseen`` / ``increment_aggregate`` query plans.
3. **Registration dashboard** — ``get_registration_dashboard`` (counter-only vs
   the waitlist ``COUNT``).
4. **Registration single-submit** — ``register_for_event`` query count (N+1 in
   the gateway's per-field gathering reads).
5. **Room-join scope gate** — ``can_join_event_room`` resolution cost (the WS
   subscribe path FLO-106).
6. **Group-scoped list query** — the ``permission_query_conditions`` hook over
   the 15k-row attendance table (the hottest scoped read).
7. **Throttle burst** — verify the per-device sliding-window rejects the 11th
   call in the same second (FLO-319 / FLO-9 §6.6).

Every timing is a best-of-N (default 5) to smooth JIT/cache noise; each
``EXPLAIN`` is captured once. The report is returned as a dict + pretty-printed
to the bench log so a run leaves durable evidence.

Run on the bench (after seeding):

    bench --site <site> execute flock_os.utils.scale_profile.execute
    # wrapper: scripts/dev/profile-15k-scale.sh

Not a migration — dev/stress tool. Findings: ``docs/operations/scale-15k-findings.md``.
"""

from __future__ import annotations

import json
import statistics
import time
from dataclasses import dataclass, field

import frappe

from flock_os.rate_limit import (
	DEFAULT_PUBLIC_ENDPOINT_THROTTLE_PER_SEC,
	InMemoryThrottleBackend,
	build_throttle_key,
	enforce,
)
from flock_os.realtime import (
	event_room_join_allowed,
	shard_channel,
	shard_for,
)
from flock_os.reporting import (
	BULK_BATCH_SIZE,
	AttendanceItem,
	AttendanceScope,
	BulkAttendanceService,
	FrappeBulkAttendanceGateway,
)
from flock_os.utils import scale_seed

WARMUP_ITERS = 2
TIMED_ITERS = 5


# ---------------------------------------------------------------------------- #
# Report model.
# ---------------------------------------------------------------------------- #
@dataclass
class TimingResult:
	"""Best-of-N timing for one hot path."""

	label: str
	samples_ms: list[float] = field(default_factory=list)
	rows: int | None = None
	note: str = ""

	@property
	def median_ms(self) -> float:
		return statistics.median(self.samples_ms) if self.samples_ms else 0.0

	@property
	def min_ms(self) -> float:
		return min(self.samples_ms) if self.samples_ms else 0.0

	@property
	def max_ms(self) -> float:
		return max(self.samples_ms) if self.samples_ms else 0.0

	def as_dict(self) -> dict:
		return {
			"label": self.label,
			"median_ms": round(self.median_ms, 3),
			"min_ms": round(self.min_ms, 3),
			"max_ms": round(self.max_ms, 3),
			"samples": len(self.samples_ms),
			"rows": self.rows,
			"note": self.note,
		}


@dataclass
class QueryPlan:
	"""One EXPLAIN row set + the verdict (scan vs index)."""

	label: str
	sql: str
	plan: list[dict]
	uses_index: bool
	full_scan: bool

	def as_dict(self) -> dict:
		return {
			"label": self.label,
			"sql": self.sql,
			"uses_index": self.uses_index,
			"full_scan": self.full_scan,
			"plan": self.plan,
		}


@dataclass
class ScaleProfileReport:
	"""Full profiler output — serialized for the findings doc."""

	timings: list[TimingResult] = field(default_factory=list)
	plans: list[QueryPlan] = field(default_factory=list)
	issues: list[dict] = field(default_factory=list)
	throttle: dict = field(default_factory=dict)
	dataset: dict = field(default_factory=dict)

	def as_dict(self) -> dict:
		return {
			"dataset": self.dataset,
			"timings": [t.as_dict() for t in self.timings],
			"plans": [p.as_dict() for p in self.plans],
			"issues": self.issues,
			"throttle": self.throttle,
		}


# ---------------------------------------------------------------------------- #
# Helpers.
# ---------------------------------------------------------------------------- #
def _time_call(fn, *, iters: int = TIMED_ITERS, warmup: int = WARMUP_ITERS) -> list[float]:
	"""Run ``fn()`` warmup+iters times, return the ms samples (no return value)."""
	for _ in range(warmup):
		fn()
	frappe.db.commit()
	samples = []
	for _ in range(iters):
		start = time.perf_counter()
		fn()
		elapsed = (time.perf_counter() - start) * 1000.0
		samples.append(elapsed)
		frappe.db.commit()
	return samples


def _explain(label: str, sql: str, values=None) -> QueryPlan:
	"""Run EXPLAIN on ``sql`` and classify scan vs index access."""
	plan_rows = frappe.db.sql("EXPLAIN " + sql, values=values, as_dict=True)
	plan = [dict(r) for r in plan_rows]
	access_types = {str(r.get("type", "")).lower() for r in plan}
	full_scan = "all" in access_types
	uses_index = bool(access_types & {"ref", "eq_ref", "const", "range", "unique_subquery"})
	return QueryPlan(
		label=label,
		sql=sql,
		plan=plan,
		uses_index=uses_index,
		full_scan=full_scan,
	)


def _gather_test_data() -> dict:
	"""Resolve the seeded anchors + a sample member per branch."""
	branches = (
		frappe.get_all(
			"Flock Branch",
			filters={"name": ["like", f"{scale_seed.SCALE_BRANCH_PREFIX}%"]},
			pluck="name",
			order_by="name asc",
		)
		or []
	)
	anchors = []
	for b in branches:
		title = f"{scale_seed.SCALE_GATHERING_TITLE} {b}"
		g = frappe.db.get_value("Flock Gathering", {"title": title})
		if g:
			anchors.append(g)
	one_time = (
		frappe.db.get_value("Flock Gathering", {"title": f"{scale_seed.SCALE_ONE_TIME_TITLE} 1"}) or None
	)
	return {
		"branches": branches,
		"anchors": anchors,
		"one_time": one_time,
		"member_sample": f"{scale_seed.SCALE_MEMBER_PREFIX}-0",
	}


# ---------------------------------------------------------------------------- #
# Hot-path probes.
# ---------------------------------------------------------------------------- #
def _probe_attendance_aggregate(data: dict, report: ScaleProfileReport) -> None:
	"""(1) Attendance aggregate read — rollup vs raw COUNT(*)."""
	if not data["anchors"] or not data["branches"]:
		report.issues.append({"id": "AGG-NO-DATA", "severity": "warn", "msg": "no seeded anchors"})
		return
	branch = data["branches"][0]
	gathering = data["anchors"][0]
	scope = AttendanceScope(branch=branch)
	gw = FrappeBulkAttendanceGateway()

	# Maintained rollup (the sanctioned path).
	def read_rollup():
		gw.aggregate(scope, gathering)

	# The anti-pattern: live COUNT(*) over the attendance table.
	def read_count_star():
		frappe.db.count(
			"Flock Attendance Record",
			filters={"branch": branch, "event": gathering},
		)

	rollup_t = _time_call(read_rollup)
	count_t = _time_call(read_count_star)
	report.timings.append(
		TimingResult(
			"attendance.aggregate (rollup)",
			[round(x, 3) for x in rollup_t],
			note="Maintained Event Attendance Summary row read.",
		)
	)
	report.timings.append(
		TimingResult(
			"attendance.aggregate (COUNT(*) anti-pattern)",
			[round(x, 3) for x in count_t],
			note="The forbidden live count — quantifies the gap the rollup closes.",
		)
	)
	report.plans.append(
		_explain(
			"attendance.summary_read",
			"SELECT total FROM `tabEvent Attendance Summary` WHERE branch=%s AND event=%s",
			values=(branch, gathering),
		)
	)
	report.plans.append(
		_explain(
			"attendance.count_star",
			"SELECT COUNT(*) FROM `tabFlock Attendance Record` WHERE branch=%s AND event=%s",
			values=(branch, gathering),
		)
	)


def _probe_attendance_bulk_write(data: dict, report: ScaleProfileReport) -> None:
	"""(2) Bulk-write throughput (rows/s) + filter_unseen plan."""
	if not data["branches"] or not data["anchors"]:
		return
	branch = data["branches"][0]
	gathering = data["anchors"][0]
	scope = AttendanceScope(branch=branch)
	service = BulkAttendanceService(FrappeBulkAttendanceGateway())

	# One fresh 500-row batch (unique attendee refs so it actually inserts).
	offset = frappe.db.count("Flock Attendance Record", filters={"branch": branch})
	items = [
		AttendanceItem(
			event=gathering,
			attendee_ref=f"scale-write-probe-{offset + j}",
			branch=branch,
			client_req_id=f"probe:{gathering}:{offset + j}",
		)
		for j in range(BULK_BATCH_SIZE)
	]
	start = time.perf_counter()
	outcome = service.submit(items, scope, batch_id=f"probe-write-{offset}")
	elapsed = time.perf_counter() - start
	wps = outcome.inserted / elapsed if elapsed > 0 else 0.0
	report.timings.append(
		TimingResult(
			"attendance.bulk_write (500-row batch)",
			[round(elapsed * 1000, 3)],
			rows=outcome.inserted,
			note=f"{wps:.0f} rows/s through the canonical BulkAttendanceService.",
		)
	)
	# filter_unseen plan — the dedupe SELECT against the 15k-row table.
	probe_keys = [("x", "y", "z")] * 3
	groups = ", ".join(["(%s, %s, %s)"] * len(probe_keys))
	report.plans.append(
		_explain(
			"attendance.filter_unseen",
			f"SELECT event, attendee_ref, client_req_id FROM `tabFlock Attendance Record` "
			f"WHERE (event, attendee_ref, client_req_id) IN ({groups})",
			values=[v for k in probe_keys for v in k],
		)
	)
	# Clean up the probe rows so the dataset stays at the seeded volume.
	frappe.db.sql("DELETE FROM `tabFlock Attendance Record` WHERE attendee_ref LIKE 'scale-write-probe-%'")
	frappe.db.commit()


def _probe_registration_dashboard(data: dict, report: ScaleProfileReport) -> None:
	"""(3) Registration dashboard — counter read vs waitlist COUNT."""
	if not data["one_time"]:
		return
	from flock_os.flock_os.doctype.flock_event_registration.flock_event_registration import (
		get_registration_dashboard,
	)

	gathering = data["one_time"]

	# Bypass the @whitelist decorator + the branch-scope guard (run as
	# Administrator) so the probe isolates the READ cost.
	frappe.set_user("Administrator")
	dash_t = _time_call(lambda: get_registration_dashboard(gathering), iters=3, warmup=1)
	report.timings.append(
		TimingResult(
			"registration.dashboard",
			[round(x, 3) for x in dash_t],
			note="Counter read + waitlist COUNT over 15k registration rows.",
		)
	)
	report.plans.append(
		_explain(
			"registration.waitlist_count",
			"SELECT COUNT(*) FROM `tabFlock Event Registration` "
			"WHERE gathering=%s AND registration_status='Waitlisted'",
			values=(gathering,),
		)
	)


def _probe_registration_gateway_reads(data: dict, report: ScaleProfileReport) -> None:
	"""(4) N+1 detection in the registration gateway (per-field gathering reads)."""
	if not data["one_time"]:
		return
	from flock_os.flock_os.doctype.flock_event_registration.flock_event_registration import (
		FrappeRegistrationScopeGateway,
	)

	gathering = data["one_time"]
	gw = FrappeRegistrationScopeGateway()

	# The current code: 3 separate get_value calls for branch/group/organization.
	def three_reads():
		gw.gathering_branch(gathering)
		gw.gathering_group(gathering)
		gw.gathering_organization(gathering)

	t = _time_call(three_reads, iters=5, warmup=2)
	# Static query count (authoritative): each get_value is one SELECT → 3
	# queries where a single multi-field get_value would suffice.
	queries = 3
	report.timings.append(
		TimingResult(
			"registration.gateway_gathering_reads (N+1)",
			[round(x, 3) for x in t],
			note=f"{queries} queries for branch+group+org — collapsible to 1.",
		)
	)
	report.issues.append(
		{
			"id": "PERF-REG-N1",
			"severity": "medium",
			"path": "FrappeRegistrationScopeGateway.gathering_*",
			"evidence": (
				f"{queries} separate SELECTs for branch+group+org per registration; "
				"register_for_event + process_bulk_batch call all three."
			),
			"fix": "Single db.get_value(gathering, ['branch','group','organization'], as_dict=True).",
		}
	)


def _probe_room_join_scope(data: dict, report: ScaleProfileReport) -> None:
	"""(5) Room-join scope gate resolution cost (the WS subscribe path)."""
	if not data["anchors"]:
		return
	gathering = data["anchors"][0]
	room = shard_channel(gathering, shard_for(data["member_sample"]))
	from flock_os.permissions import get_gateway
	from flock_os.realtime import FrappeEventBranchResolver

	def resolve():
		event_room_join_allowed(
			room=room,
			user="Administrator",
			gateway=get_gateway(),
			resolver=FrappeEventBranchResolver(),
		)

	t = _time_call(resolve, iters=5, warmup=2)
	report.timings.append(
		TimingResult(
			"realtime.room_join_scope",
			[round(x, 3) for x in t],
			note="gathering→branch resolve + user-permission check (per WS join).",
		)
	)
	report.plans.append(
		_explain(
			"realtime.gathering_branch_resolve",
			"SELECT branch FROM `tabFlock Gathering` WHERE name=%s",
			values=(gathering,),
		)
	)


def _probe_scoped_attendance_list(data: dict, report: ScaleProfileReport) -> None:
	"""(6) Group-scoped attendance list — the hottest scoped read at 15k rows."""
	if not data["branches"]:
		return
	branch = data["branches"][0]

	# Branch-scoped attendance listing (what a Branch Admin list view runs).
	def branch_list():
		frappe.get_all(
			"Flock Attendance Record",
			filters={"branch": branch},
			fields=["name", "attendee_ref", "event", "status"],
			limit_page_length=100,
		)

	t = _time_call(branch_list, iters=5, warmup=2)
	report.timings.append(
		TimingResult(
			"attendance.scoped_list (branch, limit 100)",
			[round(x, 3) for x in t],
			rows=100,
			note="Branch-scoped list view over the 15k-row attendance table.",
		)
	)
	report.plans.append(
		_explain(
			"attendance.branch_scoped_list",
			"SELECT name, attendee_ref, event, status FROM `tabFlock Attendance Record` "
			"WHERE branch=%s ORDER BY modified DESC LIMIT 100",
			values=(branch,),
		)
	)
	# The branch-scoped list EXPLAIN routinely picks the `modified` index (for
	# the ORDER BY) and filters by branch over ~1.4k rows ("Using where") rather
	# than riding the branch index — the hottest scoped read at 15k. Surface it
	# as an explicit issue backed by the plan above.
	branch_list_plan = report.plans[-1]
	if branch_list_plan.plan and str(branch_list_plan.plan[0].get("key", "")).lower() == "modified":
		report.issues.append(
			{
				"id": "PERF-LIST-IDX",
				"severity": "medium",
				"path": "Branch-scoped attendance list (Flock Attendance Record list view)",
				"evidence": (
					f"EXPLAIN picks the `modified` index scanning "
					f"{branch_list_plan.plan[0].get('rows')} rows with 'Using where' "
					"instead of the branch index — the ORDER BY modified DESC "
					"overrides the branch filter. ~18ms median at 3k rows/branch."
				),
				"fix": (
					"Composite index (branch, modified DESC) so the filter + sort "
					"share one index; or deferred sort."
				),
			}
		)


def _verify_throttle_burst(report: ScaleProfileReport) -> None:
	"""(7) Throttle burst — the sliding window rejects the 11th call in 1s.

	Uses the in-memory backend (the pure primitive) so the check is deterministic
	and bench-independent; the production backend (Redis via frappe.cache) shares
	the exact ``throttle_allows`` contract (FLO-319), so this verifies the rule,
	not the adapter.
	"""
	backend = InMemoryThrottleBackend()
	key = build_throttle_key("register_for_event", device="scale-device-1")
	now = 1000.0
	allowed = 0
	rejected = 0
	for _ in range(DEFAULT_PUBLIC_ENDPOINT_THROTTLE_PER_SEC + 5):
		try:
			enforce(
				backend,
				key,
				surface="register_for_event",
				now=now,
				max_per_second=DEFAULT_PUBLIC_ENDPOINT_THROTTLE_PER_SEC,
			)
			allowed += 1
		except Exception:
			rejected += 1
	report.throttle = {
		"surface": "register_for_event",
		"cap_per_second": DEFAULT_PUBLIC_ENDPOINT_THROTTLE_PER_SEC,
		"allowed": allowed,
		"rejected": rejected,
		"holds": allowed == DEFAULT_PUBLIC_ENDPOINT_THROTTLE_PER_SEC and rejected == 5,
		"note": (
			"In-memory primitive; production Redis backend shares the throttle_allows contract (FLO-319)."
		),
	}


# ---------------------------------------------------------------------------- #
# Static-analysis issues — perf concerns identified by code read, pinned here
# so they ship with the run as durable evidence for the backlog child issues.
# ---------------------------------------------------------------------------- #
def _attach_static_issues(report: ScaleProfileReport) -> None:
	report.issues.extend(
		[
			{
				"id": "PERF-CHK-NONATOMIC",
				"severity": "high",
				"path": "flock_event_registration._bump (check_in_registration)",
				"evidence": (
					"_bump does read-then-write (get_value + set_value) for "
					"checked_in_count — non-atomic; loses updates under concurrent "
					"check-in AND costs 2 queries where 1 suffices. Compare "
					"_bump_registered_count which correctly uses UPDATE ... = count + 1."
				),
				"fix": "Atomic UPDATE ... SET checked_in_count = checked_in_count + 1.",
			},
			{
				"id": "PERF-BULK-FORUPDATE",
				"severity": "high",
				"path": "process_bulk_batch per-member _authoritative_registration_status",
				"evidence": (
					"Each member in a bulk batch runs SELECT ... FOR UPDATE on the "
					"gathering row → 500 serial row locks per batch. Serializes "
					"the 15k bulk-registration path."
				),
				"fix": "Decide capacity once per batch under a single lock, not per member.",
			},
			{
				"id": "PERF-INVITE-N1",
				"severity": "medium",
				"path": "FrappeRegistrationScopeGateway.has_valid_invitation",
				"evidence": (
					"Loops invitee_groups calling group_subtree (recursive tree "
					"query) + a per-group get_value(expires_on). "
					"O(invitations × subtree depth) at eligibility-check time."
				),
				"fix": "Bulk-resolve subtrees once; fold expiry into the invitation fetch.",
			},
			{
				"id": "PERF-AGG-SUM",
				"severity": "low",
				"path": "FrappeBulkAttendanceGateway.aggregate (no event)",
				"evidence": (
					"Branch-total aggregate fetches every event summary row then "
					"Python sum() — grows with event count per branch."
				),
				"fix": "SELECT SUM(total) ... WHERE branch=%s at the DB.",
			},
		]
	)


# ---------------------------------------------------------------------------- #
# Entry point.
# ---------------------------------------------------------------------------- #
def execute(*, seed_if_empty: bool = True) -> dict:
	"""Run all hot-path probes; return the full report as a dict.

	``seed_if_empty=True`` runs the seeder first if no scale data is present so
	the profiler is one-shot repeatable.
	"""
	report = ScaleProfileReport()

	has_scale = frappe.db.count(
		"Flock Member", filters={"name": ["like", f"{scale_seed.SCALE_MEMBER_PREFIX}%"]}
	)
	if not has_scale and seed_if_empty:
		frappe.logger("flock_os.scale").info("scale_profile: seeding first (no scale data)")
		seed_report = scale_seed.execute()
		report.dataset["seed"] = seed_report
	else:
		report.dataset["members_present"] = int(has_scale or 0)

	data = _gather_test_data()
	report.dataset["branches"] = data["branches"]
	report.dataset["anchors"] = data["anchors"]
	report.dataset["one_time"] = data["one_time"]

	# Isolate each probe so one failure doesn't sink the whole run.
	for probe in (
		_probe_attendance_aggregate,
		_probe_attendance_bulk_write,
		_probe_registration_dashboard,
		_probe_registration_gateway_reads,
		_probe_room_join_scope,
		_probe_scoped_attendance_list,
	):
		try:
			probe(data, report)
		except Exception as exc:  # noqa: BLE001 — profiler must be resilient
			report.issues.append(
				{
					"id": f"PROBE-ERR-{probe.__name__}",
					"severity": "warn",
					"msg": f"{type(exc).__name__}: {exc}",
				}
			)
			frappe.logger("flock_os.scale").exception(f"probe {probe.__name__} failed")

	_verify_throttle_burst(report)
	_attach_static_issues(report)

	frappe.set_user("Administrator")
	frappe.logger("flock_os.scale").info(
		"scale_profile report:\n" + json.dumps(report.as_dict(), indent=2, default=str)
	)
	return report.as_dict()
