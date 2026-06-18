"""
Project-level unit tests for :mod:`flock_os.leader_reporting` — the leader
attendance-report workflow service ([FLO-56](/FLO/issues/FLO-56), spec
[FLO-6](/FLO/issues/FLO-6) §4, ADR-0001 §5).

Plain ``pytest`` (no Frappe / MariaDB / Redis). They pin the workflow contract:

* The report drives ``Held → Reported`` and refuses every other entry status
  (Scheduled / Reported / Confirmed / Cancelled / unknown).
* Visitors **and** pre-members are accepted as attendees and classified into the
  visitor roll-up; members into the member roll-up (FLO-6 rev 2 addendum).
* Unknown / inconsistent member statuses are rejected.
* Writes delegate to the canonical bulk path — per-attendee idempotency +
  ``(event, attendee_ref)`` unique backstop hold (replays never double-count),
  and the maintained aggregate reflects the inserted rows (FLO-10 §3/§4).
* ``flock.attendance.reported`` is emitted exactly once through the gateway, with
  the row-level scope + correct payload (single sanctioned emitter — no duals).
* Submissions over :data:`BULK_BATCH_SIZE` are chunked into queue-ready batches
  (the 15k path seed), with deterministic per-chunk batch ids.

The state-machine legality the service delegates to :mod:`flock_os.gatherings`
is itself pinned in :mod:`flock_os.tests.test_gatherings` (DRY — not re-tested
here beyond the workflow-level guards).
"""

from __future__ import annotations

import pytest

from flock_os import gatherings
from flock_os.events import ATTENDANCE_REPORTED
from flock_os.leader_reporting import (
	LEADER_REPORT_SOURCE,
	PRESENT_STATUS,
	REPORTED_STATUS,
	VISITOR_STATUSES,
	AttendeeReport,
	LeaderReportingError,
	LeaderReportingService,
	ReportSubmission,
)
from flock_os.reporting import BULK_BATCH_SIZE, AttendanceItem

# ---------------------------------------------------------------------------- #
# Fixtures + helpers
# ---------------------------------------------------------------------------- #


def _gateway_with(
	*,
	gathering: str = "GATH-1",
	gathering_status: str = gatherings.STATUS_HELD,
	members: dict[str, str] | None = None,
):  # noqa: F821 (forward ref to local import)
	from flock_os.leader_reporting import InMemoryLeaderReportingGateway

	gw = InMemoryLeaderReportingGateway()
	gw.register_gathering(gathering, gathering_status)
	for member, status in (members or {"m1": "Member"}).items():
		gw.register_member(member, status)
	return gw


def _submission(
	*,
	gathering: str = "GATH-1",
	branch: str = "North",
	group: str = "Youth",
	reported_by: str = "leader-1",
	attendees=None,
	client_batch_id: str = "batch-1",
) -> ReportSubmission:
	# Distinguish "not provided" (-> default m1) from an explicit empty list
	# (-> a "no one showed up" report). A bare ``attendees or [...]`` would
	# coerce [] back to the default and hide the empty-report path.
	parsed_attendees = [AttendeeReport(member="m1")] if attendees is None else list(attendees)
	return ReportSubmission(
		gathering=gathering,
		branch=branch,
		group=group,
		reported_by=reported_by,
		attendees=parsed_attendees,
		client_batch_id=client_batch_id,
	)


# ---------------------------------------------------------------------------- #
# Lifecycle guard — the report drives Held -> Reported (FLO-6 §4).
# ---------------------------------------------------------------------------- #


def test_report_transitions_held_to_reported():
	gw = _gateway_with(members={"m1": "Member"})
	service = LeaderReportingService(gw)

	outcome = service.submit_report(_submission())

	assert outcome.accepted
	assert outcome.status == REPORTED_STATUS
	# The gathering's persisted status advanced to Reported through the gateway.
	assert gw.gathering_status("GATH-1") == REPORTED_STATUS
	assert gw.advanced[0]["gathering"] == "GATH-1"
	assert gw.advanced[0]["to_status"] == REPORTED_STATUS


@pytest.mark.parametrize(
	("status", "fragment"),
	[
		(gatherings.STATUS_SCHEDULED, "Held"),
		(gatherings.STATUS_REPORTED, "Held"),
		(gatherings.STATUS_CANCELLED, "terminal"),
		(gatherings.STATUS_CONFIRMED, "terminal"),
	],
)
def test_report_rejected_when_gathering_not_held(status, fragment):
	gw = _gateway_with(gathering_status=status, members={"m1": "Member"})
	service = LeaderReportingService(gw)

	with pytest.raises(LeaderReportingError, match=fragment):
		service.submit_report(_submission())
	# No transition + no event when the guard rejects.
	assert gw.advanced == []
	assert gw.published_events == []


def test_report_rejected_when_gathering_missing():
	gw = _gateway_with(members={"m1": "Member"})
	# Drop the gathering registration to model "does not exist".
	gw._gathering_status.pop("GATH-1", None)
	service = LeaderReportingService(gw)

	with pytest.raises(LeaderReportingError, match="does not exist"):
		service.submit_report(_submission())
	assert gw.published_events == []


# ---------------------------------------------------------------------------- #
# Attendee classification — visitors / pre-members recorded as attendees
# (FLO-6 rev 2 addendum: Visitor is a Flock Member status, not a DocType).
# ---------------------------------------------------------------------------- #


def test_visitors_and_pre_members_recorded_and_classified():
	gw = _gateway_with(members={"m1": "Member", "v1": "Visitor", "p1": "Pre-Member"})
	service = LeaderReportingService(gw)

	outcome = service.submit_report(
		_submission(
			attendees=[
				AttendeeReport(member="m1"),
				AttendeeReport(member="v1"),
				AttendeeReport(member="p1"),
			]
		)
	)

	# All three produce an attendance row; classification splits member vs visitor.
	assert outcome.inserted == 3
	assert outcome.member_count == 1
	assert outcome.visitor_count == 2  # Visitor + Pre-Member both count as visitors.
	assert outcome.member_count + outcome.visitor_count == 3
	# Maintained aggregate reflects every inserted row (FLO-10 §4.2).
	assert gw.aggregate("North", "GATH-1") == 3


def test_member_status_vocabulary_marks_visitors():
	# The vocabulary is the single source of truth for the visitor roll-up split.
	assert VISITOR_STATUSES == frozenset({"Visitor", "Pre-Member"})
	assert "Member" not in VISITOR_STATUSES


def test_unknown_member_rejected():
	gw = _gateway_with(members={"m1": "Member"})  # m2 is not registered
	service = LeaderReportingService(gw)

	with pytest.raises(LeaderReportingError, match="not a known Flock Member"):
		service.submit_report(
			_submission(attendees=[AttendeeReport(member="m1"), AttendeeReport(member="m2")])
		)
	assert gw.published_events == []


def test_inconsistent_member_status_rejected():
	gw = _gateway_with(members={"m1": "Member"})
	# Simulate a corrupt member master (status outside the valid Select options).
	gw.register_member("m1", "Bogus")
	service = LeaderReportingService(gw)

	with pytest.raises(LeaderReportingError, match="unknown member status"):
		service.submit_report(_submission())


def test_absent_attendee_produces_no_row_but_counts_first_time():
	gw = _gateway_with(members={"m1": "Member", "v1": "Visitor"})
	service = LeaderReportingService(gw)

	outcome = service.submit_report(
		_submission(
			attendees=[
				AttendeeReport(member="m1", present=True, first_time=True),
				AttendeeReport(member="v1", present=False, first_time=True),
			]
		)
	)

	# Only the present attendee is written; the absent visitor is not recorded.
	assert outcome.inserted == 1
	assert outcome.member_count == 1
	assert outcome.visitor_count == 0
	# first_time counts both flagged attendees for the roll-up (a first-time
	# visitor who was absent still matters for follow-up — FLO-6 §3.3).
	assert outcome.first_time_count == 2


# ---------------------------------------------------------------------------- #
# Idempotency — delegates to the canonical bulk path (FLO-10 §3 / FLO-15).
# ---------------------------------------------------------------------------- #


def test_replayed_submission_deduplicates_via_unique_backstop():
	gw = _gateway_with(members={"m1": "Member", "v1": "Visitor"})
	service = LeaderReportingService(gw)
	attendees = [
		AttendeeReport(member="m1", client_req_id="r1"),
		AttendeeReport(member="v1", client_req_id="r2"),
	]

	first = service.submit_report(_submission(attendees=attendees, client_batch_id="batch-1"))
	# Second submission for the SAME gathering would normally need the gathering
	# back in Held; we re-arm Held to exercise the dedupe against the prior rows.
	gw.register_gathering("GATH-1", gatherings.STATUS_HELD)
	second = service.submit_report(_submission(attendees=attendees, client_batch_id="batch-2"))

	assert first.inserted == 2
	# The ``(event, attendee_ref)`` unique backstop dedupes the replayed rows.
	assert second.deduplicated == 2
	assert second.inserted == 0
	# Aggregate never double-counts (FLO-10 §4).
	assert gw.aggregate("North", "GATH-1") == 2


def test_items_carry_leader_source_and_present_status():
	gw = _gateway_with(members={"m1": "Member"})
	# Capture the items handed to the bulk path by wrapping record_attendance.
	recorded: list[list[AttendanceItem]] = []

	def spy(items, scope, batch_id):  # noqa: ANN001
		recorded.append(list(items))
		return gw._bulk_service.submit(items, scope, batch_id).inserted, 0

	gw.record_attendance = spy  # type: ignore[method-assign]
	LeaderReportingService(gw).submit_report(_submission())

	handed = recorded[0][0]
	assert handed.source == LEADER_REPORT_SOURCE
	assert handed.status == PRESENT_STATUS
	assert handed.event == "GATH-1"
	assert handed.branch == "North"
	# client_req_id falls back to the member ref when the caller omits it.
	assert handed.client_req_id == "m1"
	assert handed.attendee_ref == "m1"


# ---------------------------------------------------------------------------- #
# Chunking — the 15k-scale queue path seed (FLO-10 §3.2).
# ---------------------------------------------------------------------------- #


def test_submission_over_batch_size_is_chunked():
	members = {f"m{i}": "Member" for i in range(BULK_BATCH_SIZE + 5)}
	gw = _gateway_with(members=members)
	attendees = [AttendeeReport(member=f"m{i}") for i in range(BULK_BATCH_SIZE + 5)]

	outcome = LeaderReportingService(gw).submit_report(
		_submission(attendees=attendees, client_batch_id="big")
	)

	# 500 + 5 -> two deterministic, queue-ready batch ids.
	assert outcome.batch_ids == ["big#0", "big#1"]
	assert outcome.inserted == BULK_BATCH_SIZE + 5
	# The maintained aggregate reflects every chunk (no rows lost at the seam).
	assert gw.aggregate("North", "GATH-1") == BULK_BATCH_SIZE + 5


def test_empty_report_is_allowed_and_advances_gathering():
	# A "no one showed up" report is still a valid report — the gathering
	# transitions to Reported with zero counts, and one event is emitted.
	gw = _gateway_with(members={})
	outcome = LeaderReportingService(gw).submit_report(_submission(attendees=[]))

	assert outcome.accepted
	assert outcome.inserted == 0
	assert outcome.member_count == 0
	assert outcome.visitor_count == 0
	assert gw.gathering_status("GATH-1") == REPORTED_STATUS
	assert len(gw.published_events) == 1


# ---------------------------------------------------------------------------- #
# Event emission — single canonical emit (ADR-0001 §5.1, no dual emitters).
# ---------------------------------------------------------------------------- #


def test_attendance_reported_emitted_once_with_scope_and_payload():
	gw = _gateway_with(members={"m1": "Member", "v1": "Visitor"})
	service = LeaderReportingService(gw)

	service.submit_report(
		_submission(
			attendees=[
				AttendeeReport(member="m1"),
				AttendeeReport(member="v1", first_time=True),
			]
		)
	)

	assert len(gw.published_events) == 1
	name, payload, scope = gw.published_events[0]
	assert name == ATTENDANCE_REPORTED
	# Row-level scope is carried so subscribers/realtime derive rooms without
	# re-querying (event-modeling rule).
	assert scope == {"branch": "North", "group": "Youth"}
	# Payload carries the gathering + roll-up counts + reporter + idempotency key.
	assert payload["gathering"] == "GATH-1"
	assert payload["reported_by"] == "leader-1"
	assert payload["member_count"] == 1
	assert payload["visitor_count"] == 1
	assert payload["total_count"] == 2
	assert payload["first_time_count"] == 1
	assert payload["client_batch_id"] == "batch-1"


def test_no_event_emitted_when_validation_fails():
	gw = _gateway_with(gathering_status=gatherings.STATUS_SCHEDULED, members={"m1": "Member"})
	with pytest.raises(LeaderReportingError):
		LeaderReportingService(gw).submit_report(_submission())
	assert gw.published_events == []


# ---------------------------------------------------------------------------- #
# Submission shape validation.
# ---------------------------------------------------------------------------- #


@pytest.mark.parametrize(
	("kwargs", "fragment"),
	[
		({"gathering": ""}, "gathering"),
		({"branch": ""}, "branch"),
		({"group": ""}, "group"),
		({"reported_by": ""}, "reported_by"),
		({"client_batch_id": ""}, "client_batch_id"),
	],
)
def test_submission_shape_validation(kwargs, fragment):
	gw = _gateway_with(members={"m1": "Member"})
	with pytest.raises(LeaderReportingError, match=fragment):
		LeaderReportingService(gw).submit_report(_submission(**kwargs))


def test_advance_carries_rollup_counters():
	gw = _gateway_with(members={"m1": "Member", "v1": "Visitor"})
	LeaderReportingService(gw).submit_report(
		_submission(
			attendees=[
				AttendeeReport(member="m1", first_time=True),
				AttendeeReport(member="v1", first_time=True),
			]
		)
	)
	advanced = gw.advanced[0]
	assert advanced["member_count"] == 1
	assert advanced["visitor_count"] == 1
	assert advanced["first_time_count"] == 2
	assert advanced["reported_by"] == "leader-1"
