"""
Transport layer for bulk attendance reporting (FLO-15).

Exposes the queue-based write path from FLO-10 §3 as a Frappe whitelisted REST
endpoint and the matching background job. **No domain rules live here** — this
module only parses input, resolves the caller's org-tree branch once per batch,
hands a receipt back immediately, and delegates persistence to
:class:`flock_os.reporting.BulkAttendanceService` via an RQ job.

REST contract (FLO-10 §3.2):

    POST /api/method/flock_os.attendance.bulk_submit
      body: {
        event:   "<Flock Gathering id>",
        items:   [{ attendee_ref, status?, source?, client_req_id }],
        batch_id: "<client-supplied batch correlation id>"
      }
      200: { accepted: bool, queued: int, rejected: [{index, reason}], batch_id }

Per the ADR a batch is scoped to a single event/branch; every item inherits the
caller's resolved branch (the org-tree node / row-level permission anchor). The
actual inserts happen in the queue job (:func:`process_bulk_batch`); the reporter
receives a *receipt*, not a synchronous commit (D1 — attendance is
eventually-consistent to the reporter, surfaced via realtime).
"""

from __future__ import annotations

from typing import Any

import frappe

from flock_os.reporting import (
	ATTENDANCE_IMPORT_ERROR_QUEUE,
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


def process_bulk_batch(payload: dict[str, Any]) -> None:
	"""RQ job: persist one batch + update aggregates + emit events (FLO-10 §3.3).

	Idempotent by construction (per-item ``client_req_id`` dedupe), so RQ retries
	are always safe. On failure after :data:`BULK_ATTENDANCE_MAX_RETRY`, the batch
	is dead-lettered to the visible :data:`ATTENDANCE_IMPORT_ERROR_QUEUE` queue.
	"""
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
	"""Dead-letter a batch after max retries, else re-enqueue with backoff (FLO-10 §3.3)."""
	attempt = int(payload.get("_attempt", 0)) + 1
	batch_id = payload["batch_id"]
	if attempt > BULK_ATTENDANCE_MAX_RETRY:
		FrappeBulkAttendanceGateway().emit(
			DomainEvent(
				EVENT_IMPORT_FAILED,
				{"batch_id": batch_id, "attempts": attempt - 1, "reason": "max_retry_exceeded"},
			)
		)
		frappe.enqueue(
			"flock_os.attendance.process_bulk_batch",
			queue=ATTENDANCE_IMPORT_ERROR_QUEUE,
			payload={**payload, "_attempt": attempt, "_deadlettered": True},
		)
		return
	payload["_attempt"] = attempt
	frappe.enqueue(
		"flock_os.attendance.process_bulk_batch",
		queue=BULK_ATTENDANCE_JOB_QUEUE,
		payload=payload,
		at=_now_seconds() + with_exponential_backoff(attempt - 1),
	)


def _now_seconds() -> int:
	import time

	return int(time.time())
