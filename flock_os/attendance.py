"""
Transport layer for attendance reporting.

Two REST surfaces live here, both thin (no domain rules — ADR-0001 §2):

1. **Bulk attendance reporting** (FLO-15 / FLO-10 §3) — ``bulk_submit`` accepts
   one queue-ready batch, enqueues it, and returns a receipt. Background job
   ``process_bulk_batch`` does the durable write.
2. **Leader attendance-report workflow** (FLO-56 / FLO-6 §4) — ``report_attendance``
   submits one leader report (members + visitors / pre-members) over a
   ``Flock Gathering``; it delegates to :class:`LeaderReportingService`, which
   records attendees through the same bulk write path, drives the
   ``Held → Reported`` transition, and emits ``flock.attendance.reported``.

Both paths resolve the caller's org-tree branch **once per request** (set-based,
D7) via :func:`_resolve_caller_branch_scope` and stamp it on every row — the
row-level permission anchor (ADR-0001 §6.2). Batches are durable the moment RQ
accepts the enqueue; the queue owns correctness (idempotent retries, dead-letter,
atomic aggregates).
"""

from __future__ import annotations

from typing import Any

import frappe

# ``AttendeeReport`` is a pure dataclass (no Frappe coupling), so a module-level
# import is safe and lets the transport parse helper annotate its return type
# without a lazy import (the rest of the leader workflow stays lazy-imported in
# ``report_attendance`` to keep the transport boundary explicit).
from flock_os.leader_reporting import AttendeeReport  # noqa: E402
from flock_os.reporting import (
	BULK_ATTENDANCE_JOB_QUEUE,
	BULK_ATTENDANCE_MAX_RETRY,
	EVENT_IMPORT_FAILED,
	AttendanceItem,
	AttendanceScope,
	BatchSizeExceeded,
	BulkAttendanceService,
	DomainEvent,
	FrappeBulkAttendanceGateway,
	enforce_batch_size,
	with_exponential_backoff,
)
from flock_os.telemetry import measure_bulk_latency

DEFAULT_ATTENDANCE_STATUS = "Present"
DEFAULT_ATTENDANCE_SOURCE = "bulk"

# Leader workflow source classification (FLO-56). Visitors / pre-members are
# accepted as attendees — classification is owned by the pure service.
_LEADER_ATTENDEE_DEFAULT_PRESENT = True
_LEADER_ATTENDEE_DEFAULT_FIRST_TIME = False


@frappe.whitelist()
def bulk_submit(event: str, items: list[dict[str, Any]], batch_id: str) -> dict[str, Any]:
	"""Accept one attendance batch (≤ :data:`BULK_BATCH_SIZE`), enqueue it, return a receipt.

	Resolves the caller's org-tree branch **once per batch** (set-based, D7) and
	stamps it on every item — the row-level permission anchor (ADR-0001 §6.2).
	Batches are durable the moment RQ accepts the enqueue; the queue owns
	correctness (idempotent retries, dead-letter, atomic aggregates).
	"""
	if not event:
		frappe.throw("event is required")
	if not batch_id:
		frappe.throw("batch_id is required")
	if not isinstance(items, list):
		frappe.throw("items must be a list")

	# The synchronous receipt path is the §8 p95 < 500ms surface (FLO-10 §5.3:
	# reporter ack is cheap + synchronous). Telemetry observes its latency so the
	# k6 smoke + dashboards read the gate directly (FLO-49).
	with measure_bulk_latency():
		scope = _resolve_caller_branch_scope()
		attendance_items = [
			_item_from_payload(event, scope.branch, raw, index) for index, raw in enumerate(items)
		]

		try:
			enforce_batch_size(attendance_items)
		except BatchSizeExceeded as exc:
			frappe.throw(str(exc), title="Batch too large")

		frappe.enqueue(
			"flock_os.attendance.process_bulk_batch",
			queue=BULK_ATTENDANCE_JOB_QUEUE,
			payload={
				"event": event,
				"batch_id": batch_id,
				"scope_branch": scope.branch,
				"items": [
					{
						"event": item.event,
						"attendee_ref": item.attendee_ref,
						"branch": item.branch,
						"status": item.status,
						"source": item.source,
						"client_req_id": item.client_req_id,
					}
					for item in attendance_items
				],
			},
		)
		return {
			"accepted": True,
			"queued": len(attendance_items),
			"rejected": [],
			"batch_id": batch_id,
		}


def process_bulk_batch(payload: dict[str, Any], at: int | None = None) -> None:
	"""RQ job: persist one batch + update aggregates + emit events (FLO-10 §3.3).

	Idempotent by construction (per-item ``client_req_id`` dedupe), so RQ retries
	are always safe. On failure after :data:`BULK_ATTENDANCE_MAX_RETRY`, the batch
	is dead-lettered to Frappe's ``Error Log`` + a ``flock.attendance.import_failed``
	event (FLO-76) -- no separate dead-letter queue, so there is no re-drain loop.

	``at`` is the backoff-schedule timestamp forwarded by ``frappe.enqueue`` on
	retry (RQ passes enqueue kwargs through to the job); the delay has already
	elapsed by the time the worker runs the job, so it is ignored here.
	"""
	# Defensive guard: a stale dead-lettered job still sitting in a queue must not
	# re-enter the write path -- just log + return (FLO-76 loop-safety).
	if payload.get("_deadlettered"):
		_log_deadletter(payload, reason="stale_deadletter_drained")
		return

	items = [
		AttendanceItem(
			event=raw["event"],
			attendee_ref=raw["attendee_ref"],
			branch=raw["branch"],
			status=raw.get("status", DEFAULT_ATTENDANCE_STATUS),
			source=raw.get("source", DEFAULT_ATTENDANCE_SOURCE),
			client_req_id=raw["client_req_id"],
		)
		for raw in payload["items"]
	]
	scope = AttendanceScope(branch=payload["scope_branch"])
	batch_id = payload["batch_id"]

	try:
		service = BulkAttendanceService(FrappeBulkAttendanceGateway())
		outcome = service.submit(items, scope, batch_id)
	except Exception:
		# Unique-index backstop / transient DB error: idempotent retry is safe.
		_deadletter_or_retry(payload)
		return

	from flock_os.events import emit as emit_event

	emit_event(
		f"flock_os:attendance:batch:{batch_id}",
		payload={
			"accepted": outcome.accepted,
			"inserted": outcome.inserted,
			"deduplicated": outcome.deduplicated,
			"rejected": outcome.rejected_count,
		},
		# Per-batch realtime ack for the submitting reporter (FLO-10 §5.3: reporter
		# ack cheap + synchronous). Routed through the canonical emitter so the
		# single-publisher rule holds uniformly and the receipt lands in the
		# ``Flock Event Outbox`` for replay (ADR-0001 §5.1). This is a transport
		# UX channel, not a cataloged domain event — the realtime projector ignores
		# un-cataloged names (no shard fan-out); only the sink publishes + persists.
		# ``room=None`` preserves the prior broadcast-on-event-name semantics: the
		# reporter's browser listens on this per-batch event name for correlation.
		room=None,
	)


# ---------------------------------------------------------------------------- #
# Internals
# ---------------------------------------------------------------------------- #
def _item_from_payload(event: str, branch: str, raw: dict[str, Any], index: int) -> AttendanceItem:
	attendee_ref = raw.get("attendee_ref")
	if not attendee_ref:
		frappe.throw(f"items[{index}].attendee_ref is required")
	client_req_id = raw.get("client_req_id")
	if not client_req_id:
		frappe.throw(f"items[{index}].client_req_id is required (idempotency)")
	return AttendanceItem(
		event=event,
		attendee_ref=str(attendee_ref),
		branch=branch,
		status=raw.get("status", DEFAULT_ATTENDANCE_STATUS),
		source=raw.get("source", DEFAULT_ATTENDANCE_SOURCE),
		client_req_id=str(client_req_id),
	)


def _resolve_caller_branch_scope() -> AttendanceScope:
	"""Resolve the caller's org-tree branch once per batch (ADR-0001 §6.2).

	The branch axis rides native Frappe User Permissions on ``Flock Branch``. The
	canonical bulk-reporting caller is a group leader / branch admin scoped to a
	single branch; that branch becomes the batch scope. Cross-branch (org-admin)
	bulk reporting is served by the native permission model per-item and is not
	the 15k-scale hot path, so it is rejected here with a clear message.
	"""
	allowed = frappe.permissions.get_user_permissions(frappe.session.user).get("Flock Branch", [])
	branches = {perm.get("doc") for perm in allowed if perm.get("doc")}
	if len(branches) == 1:
		return AttendanceScope(branch=next(iter(branches)))
	frappe.throw(
		"bulk_submit requires a single Flock Branch User Permission scope "
		"(the leader / branch-admin case). Cross-branch reporting is not supported "
		"on this path.",
		title="Ambiguous branch scope",
	)


def _deadletter_or_retry(payload: dict[str, Any]) -> None:
	"""Retry a failed batch with exponential backoff, or dead-letter it (FLO-10 §3.3).

	Retries re-enqueue on the stock ``long`` queue (BULK_ATTENDANCE_JOB_QUEUE) with
	backoff. Once retries are exhausted the batch is dead-lettered: a
	``flock.attendance.import_failed`` event is emitted and the payload is recorded
	in Frappe's ``Error Log`` for inspection/replay (FLO-76). There is deliberately
	no separate dead-letter queue re-enqueue -- a queue a worker drains would re-run
	the failing batch forever; the Error Log + failure event are the durable surface.
	"""
	attempt = int(payload.get("_attempt", 0)) + 1
	batch_id = payload["batch_id"]
	if attempt > BULK_ATTENDANCE_MAX_RETRY:
		FrappeBulkAttendanceGateway().emit(
			DomainEvent(
				EVENT_IMPORT_FAILED,
				{"batch_id": batch_id, "attempts": attempt - 1, "reason": "max_retry_exceeded"},
			)
		)
		_log_deadletter(payload, reason="max_retry_exceeded")
		return
	payload["_attempt"] = attempt
	# Schedule the retry on the stock `long` queue with exponential backoff
	# (FLO-10 §3.3). `at` is the absolute Unix timestamp to run at; RQ forwards
	# enqueue kwargs to the job, so `process_bulk_batch` accepts (and ignores)
	# `at` — by the time the worker runs the job the backoff has elapsed.
	delay = with_exponential_backoff(attempt - 1)
	frappe.enqueue(
		"flock_os.attendance.process_bulk_batch",
		queue=BULK_ATTENDANCE_JOB_QUEUE,
		payload=payload,
		at=_now_seconds() + int(delay),
	)


def _log_deadletter(payload: dict[str, Any], *, reason: str) -> None:
	"""Record a dead-lettered batch in Frappe's Error Log (the durable surface)."""
	try:
		frappe.log_error(
			title=f"flock_os.attendance dead-letter ({reason}): {payload.get('batch_id')}",
			message=str(payload),
		)
	except Exception:  # noqa: BLE001 - dead-letter logging must never mask the failure
		pass


def _now_seconds() -> int:
	import time

	return int(time.time())


# ---------------------------------------------------------------------------- #
# Leader attendance-report workflow transport (FLO-56 / FLO-6 §4)
# ---------------------------------------------------------------------------- #
# Thin REST surface over :class:`flock_os.leader_reporting.LeaderReportingService`.
# No domain rules live here — input parsing, caller-scope resolution, gathering
# authz, and the dict receipt only. The service owns the workflow; the Frappe
# gateway owns the bulk write + transition + canonical emit.
#
# REST contract:
#
#     POST /api/method/flock_os.attendance.report_attendance
#       body: {
#         gathering:     "<Flock Gathering id>",
#         attendees:     [{ member, present?, first_time?, client_req_id? }],
#         client_batch_id: "<caller-supplied submission idempotency key>"
#       }
#       200: {
#         accepted: bool, status: "Reported",
#         inserted, deduplicated, member_count, visitor_count,
#         first_time_count, batch_ids: [...]
#       }
# ---------------------------------------------------------------------------- #
@frappe.whitelist()
def report_attendance(
	gathering: str,
	attendees: list[dict[str, Any]],
	client_batch_id: str,
) -> dict[str, Any]:
	"""Submit one leader attendance report for a gathering (FLO-56 / FLO-6 §4).

	Resolves the caller's branch once (D7), authorizes the gathering is in the
	caller's branch and ``Held``, then delegates to the leader-report service.
	Attendees may be members, visitors, or pre-members (all ``Flock Member``
	refs); the service classifies + records them through the bulk write path and
	emits ``flock.attendance.reported``.
	"""
	from flock_os.leader_reporting import (
		REPORTABLE_STATUS,
		FrappeLeaderReportingGateway,
		LeaderReportingError,
		LeaderReportingService,
		ReportSubmission,
	)

	if not gathering:
		frappe.throw("gathering is required")
	if not client_batch_id:
		frappe.throw("client_batch_id is required (idempotency)")
	if not isinstance(attendees, list):
		frappe.throw("attendees must be a list")

	scope = _resolve_caller_branch_scope()
	doc = frappe.get_doc("Flock Gathering", gathering)  # row-level perm scoped (ADR §6.2/§6.3).
	if doc.branch != scope.branch:
		frappe.throw(
			"Gathering is not in the caller's branch scope (row-level isolation, ADR §6.2).",
			frappe.PermissionError,
		)
	if doc.status != REPORTABLE_STATUS:
		frappe.throw(
			f"Gathering is {doc.status!r}; a report requires {REPORTABLE_STATUS!r} (FLO-6 §4).",
			title="Not reportable yet",
		)

	submission = ReportSubmission(
		gathering=gathering,
		branch=scope.branch,
		group=doc.group,
		reported_by=_resolve_reporter_member(),
		attendees=[_attendee_from_payload(raw, index) for index, raw in enumerate(attendees)],
		client_batch_id=client_batch_id,
	)

	try:
		outcome = LeaderReportingService(FrappeLeaderReportingGateway()).submit_report(submission)
	except LeaderReportingError as exc:
		frappe.throw(str(exc), title="Attendance report rejected")

	return {
		"accepted": outcome.accepted,
		"status": outcome.status,
		"inserted": outcome.inserted,
		"deduplicated": outcome.deduplicated,
		"member_count": outcome.member_count,
		"visitor_count": outcome.visitor_count,
		"first_time_count": outcome.first_time_count,
		"batch_ids": outcome.batch_ids,
	}


def _attendee_from_payload(raw: dict[str, Any], index: int) -> AttendeeReport:
	"""Parse one attendee payload into an :class:`AttendeeReport` (transport parse only)."""
	member = raw.get("member")
	if not member:
		frappe.throw(f"attendees[{index}].member is required")
	return AttendeeReport(
		member=str(member),
		present=bool(raw.get("present", _LEADER_ATTENDEE_DEFAULT_PRESENT)),
		first_time=bool(raw.get("first_time", _LEADER_ATTENDEE_DEFAULT_FIRST_TIME)),
		client_req_id=str(raw["client_req_id"]) if raw.get("client_req_id") else None,
	)


def _resolve_reporter_member() -> str:
	"""Resolve the caller's ``Flock Member`` ref (the report's ``reported_by``).

	The reporter is the ``Flock Member`` linked to the session user. Falls back
	to the session user's email when no member link is recorded so the workflow
	still functions during bootstrap / test scaffolding.
	"""
	member = frappe.db.get_value("Flock Member", {"linked_user": frappe.session.user}, "name")
	return member or frappe.session.user
