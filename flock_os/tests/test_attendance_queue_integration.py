"""
Live-bench integration tests for the bulk-attendance queue path (FLO-76).

Plain pytest (no Frappe / MariaDB / Redis). Because ``flock_os.attendance``
imports ``frappe`` at module top level (for ``@frappe.whitelist()``), a minimal
fake ``frappe`` is installed into ``sys.modules`` *before* the import and reused.
These pin the **root-cause fix**: ``frappe.enqueue(queue=...)`` is validated by
``validate_queue`` against the ``@lru_cache``-d ``get_queues_timeout()``, which a
long-running *web* process can resolve to just ``short/default/long``. The custom
``flock_attendance`` queue was therefore rejected in the web process (FLO-15
live-bench failure); FLO-76 rides the stock ``long`` queue so the enqueue target
is valid in **every** runtime context.
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest


class _FakeFrappe:
	@staticmethod
	def whitelist(*_args: Any, **_kwargs: Any):
		def _decorator(func):
			return func

		return _decorator

	def __init__(self) -> None:
		self.session = types.SimpleNamespace(user="leader@flock.os")
		self.permissions = _FakePermissions()
		self.enqueue_calls: list[dict[str, Any]] = []
		self.log_error_calls: list[dict[str, Any]] = []
		self.utils = types.SimpleNamespace(get_traceback=lambda: "TRACEBACK")

	def reset(self) -> None:
		self.enqueue_calls.clear()
		self.log_error_calls.clear()

	def enqueue(self, method: str, **kwargs: Any) -> None:
		self.enqueue_calls.append({"method": method, **kwargs})

	def log_error(self, *, title: str, message: str = "") -> None:
		self.log_error_calls.append({"title": title, "message": message})

	def throw(self, msg: str, **_kwargs: Any) -> None:
		raise ValueError(msg)


class _FakePermissions:
	def get_user_permissions(self, _user: str) -> dict[str, list[dict[str, Any]]]:
		return {"Flock Branch": [{"doc": "branch-smoke"}]}


_FRAPPE_STUB = _FakeFrappe()
sys.modules.setdefault("frappe", _FRAPPE_STUB)

from flock_os import attendance  # noqa: E402
from flock_os.reporting import (  # noqa: E402
	BULK_ATTENDANCE_JOB_QUEUE,
	BULK_ATTENDANCE_MAX_RETRY,
)

_FRAPPE_STOCK_QUEUES = {"short", "default", "long"}


@pytest.fixture
def fake_frappe():
	_FRAPPE_STUB.reset()
	return _FRAPPE_STUB


def test_bulk_attendance_queue_is_a_stock_frappe_queue() -> None:
	"""The bulk queue must be one Frappe validates in every runtime context (FLO-76)."""
	assert BULK_ATTENDANCE_JOB_QUEUE in _FRAPPE_STOCK_QUEUES


def test_bulk_attendance_queue_survives_minimal_queue_cache() -> None:
	"""A stale web process (only stock queues cached) still accepts the enqueue."""
	assert BULK_ATTENDANCE_JOB_QUEUE in {"short", "default", "long"}


def test_bulk_submit_enqueues_on_standard_queue(fake_frappe, monkeypatch) -> None:
	"""bulk_submit hands the batch to frappe.enqueue on the stock long queue."""
	fake_scope = attendance.AttendanceScope(branch="branch-smoke")
	monkeypatch.setattr(attendance, "_resolve_caller_branch_scope", lambda: fake_scope)

	items = [{"attendee_ref": f"m-{i}", "client_req_id": f"b:{i}", "status": "Present"} for i in range(3)]
	receipt = attendance.bulk_submit(event="gathering-smoke", items=items, batch_id="batch-1")

	assert receipt == {"accepted": True, "queued": 3, "rejected": [], "batch_id": "batch-1"}
	assert len(fake_frappe.enqueue_calls) == 1
	call = fake_frappe.enqueue_calls[0]
	assert call["method"] == "flock_os.attendance.process_bulk_batch"
	assert call["queue"] == BULK_ATTENDANCE_JOB_QUEUE == "long"
	assert call["payload"]["batch_id"] == "batch-1"


# --------------------------------------------------------------------------- #
# Dead-letter / retry path (FLO-76: Error Log, no separate re-drained queue)
# --------------------------------------------------------------------------- #


def _payload(*, attempt: int = 0, batch_id: str = "batch-x") -> dict[str, Any]:
	return {
		"event": "gathering-smoke",
		"batch_id": batch_id,
		"scope_branch": "branch-smoke",
		"items": [
			{
				"event": "gathering-smoke",
				"attendee_ref": "m-0",
				"branch": "branch-smoke",
				"status": "Present",
				"source": "bulk",
				"client_req_id": "b:m-0",
			}
		],
		"_attempt": attempt,
	}


def test_deadletter_retry_reenqueues_on_standard_queue(fake_frappe) -> None:
	"""A retryable failure re-enqueues on the stock long queue with backoff."""
	attendance._deadletter_or_retry(_payload(attempt=0))
	assert len(fake_frappe.enqueue_calls) == 1
	call = fake_frappe.enqueue_calls[0]
	assert call["queue"] == BULK_ATTENDANCE_JOB_QUEUE == "long"
	assert call["payload"]["_attempt"] == 1
	assert call["at"] > 0
	assert fake_frappe.log_error_calls == []


def test_deadletter_max_retry_logs_and_emits_without_reenqueue(fake_frappe) -> None:
	"""Past max retries: Error Log + failure event, no re-enqueue (no re-drain loop)."""
	from flock_os import events as flock_events
	from flock_os.events import NullEventSink, RecordingEventSink, install_sink

	sink = RecordingEventSink()
	install_sink(sink)
	try:
		attendance._deadletter_or_retry(_payload(attempt=BULK_ATTENDANCE_MAX_RETRY))
	finally:
		install_sink(NullEventSink())

	# No re-enqueue on dead-letter (the old separate-queue re-enqueue could loop).
	assert fake_frappe.enqueue_calls == []
	assert len(fake_frappe.log_error_calls) == 1
	assert "dead-letter" in fake_frappe.log_error_calls[0]["title"]
	assert any(pub.name == flock_events.ATTENDANCE_IMPORT_FAILED for pub, _rt, _room in sink.published)


def test_process_bulk_batch_deadlettered_guard_short_circuits(fake_frappe) -> None:
	"""A stale dead-lettered job drained from a queue must not re-enter the write path."""
	attendance.process_bulk_batch({**_payload(), "_deadlettered": True})
	assert fake_frappe.enqueue_calls == []
	assert len(fake_frappe.log_error_calls) == 1


def test_process_bulk_batch_surfaces_failure_traceback_before_retry(fake_frappe, monkeypatch) -> None:
	"""FLO-100: a persistence failure logs its real traceback before the retry.

	The prior bare ``except: _deadletter_or_retry`` swallowed the cause, making
	the 200-wps concurrency failure (InnoDB lock-wait on the shared summary row)
	invisible. Now the class + message land in the Error Log ``error`` field,
	and the idempotent retry is still scheduled.
	"""

	def _boom_service(*_args: Any, **_kwargs: Any) -> Any:
		raise RuntimeError("lock wait timeout simulated")

	monkeypatch.setattr(attendance, "BulkAttendanceService", _boom_service)

	attendance.process_bulk_batch(_payload(attempt=0))

	# The real exception is surfaced with its traceback…
	assert len(fake_frappe.log_error_calls) == 1
	entry = fake_frappe.log_error_calls[0]
	assert "persistence failed" in entry["title"]
	assert entry["message"] == "TRACEBACK"
	# …and the idempotent retry is still scheduled on the stock long queue.
	assert len(fake_frappe.enqueue_calls) == 1
	call = fake_frappe.enqueue_calls[0]
	assert call["queue"] == "long"
	assert call["payload"]["_attempt"] == 1


# --------------------------------------------------------------------------- #
# In-place retry of transient concurrency errors (FLO-100: §8 queue drain)
# --------------------------------------------------------------------------- #
class _TransientDBError(Exception):
	"""Stand-in for frappe.QueryDeadlockError (MariaDB 1020) in unit tests."""


def _flaky_service_factory(fail_times: int) -> tuple[Any, dict[str, int]]:
	"""Build a BulkAttendanceService fake that fails ``fail_times`` then succeeds."""
	state = {"calls": 0}

	class _FlakyService:
		def __init__(self, _gateway: Any) -> None:
			pass

		def submit(self, _items: Any, _scope: Any, _batch_id: Any) -> Any:
			state["calls"] += 1
			if state["calls"] <= fail_times:
				raise _TransientDBError("1020 simulated on summary row")
			return types.SimpleNamespace(accepted=True, inserted=1, deduplicated=0, rejected_count=0)

	return _FlakyService, state


def test_process_bulk_batch_retries_transient_error_in_place_then_succeeds(fake_frappe, monkeypatch) -> None:
	"""FLO-100: a transient DB concurrency error (MariaDB 1020 on the shared
	summary row) is retried in-place until the contended X-lock frees, so the
	batch persists without flooding the slow backoff re-enqueue path."""
	flaky, state = _flaky_service_factory(fail_times=2)
	monkeypatch.setattr(attendance, "BulkAttendanceService", flaky)
	monkeypatch.setattr(attendance, "_retryable_persistence_errors", lambda: (_TransientDBError,))
	monkeypatch.setattr(attendance, "_sleep_in_place_backoff", lambda _attempt: None)

	attendance.process_bulk_batch(_payload(attempt=0))

	# The job succeeded on the 3rd attempt (2 in-place retries).
	assert state["calls"] == 3
	# No dead-letter / backoff re-enqueue, and no error logged.
	assert fake_frappe.enqueue_calls == []
	assert fake_frappe.log_error_calls == []


def test_process_bulk_batch_deadletters_after_exhausting_in_place_retries(fake_frappe, monkeypatch) -> None:
	"""FLO-100: once in-place retries are exhausted on a transient error, the
	batch is logged (with the attempt count) and falls through to the backoff
	re-enqueue — the slow path that was the only path before this change."""

	class _AlwaysFails:
		def __init__(self, _gateway: Any) -> None:
			pass

		def submit(self, _items: Any, _scope: Any, _batch_id: Any) -> Any:
			raise _TransientDBError("1020 simulated")

	monkeypatch.setattr(attendance, "BulkAttendanceService", _AlwaysFails)
	monkeypatch.setattr(attendance, "_retryable_persistence_errors", lambda: (_TransientDBError,))
	monkeypatch.setattr(attendance, "_sleep_in_place_backoff", lambda _attempt: None)

	attendance.process_bulk_batch(_payload(attempt=0))

	# Logged with the exhausted-retries title, then the idempotent backoff re-enqueue.
	assert len(fake_frappe.log_error_calls) == 1
	assert "retries" in fake_frappe.log_error_calls[0]["title"]
	assert len(fake_frappe.enqueue_calls) == 1
	assert fake_frappe.enqueue_calls[0]["queue"] == "long"


# --------------------------------------------------------------------------- #
# ADR §9 / FLO-199: manual-roster provenance stamping + cross-source dedup
#
# The manual-roster bulk path must stamp the same ``(gathering, member)``
# provenance the engagement close path stamps (FLO-195) so the
# ``UNIQUE (branch, gathering, member)`` index collapses a manual credit + an
# engagement credit for the same member into one attendance row. Visitors omit
# ``member`` so a dangling ref never lands in the FK column.
# --------------------------------------------------------------------------- #
def test_item_from_payload_stamps_gathering_and_member_for_member(fake_frappe) -> None:
	"""A manual-roster member row stamps ``gathering=event`` + ``member=member_ref``."""
	item = attendance._item_from_payload(
		event="gathering-1",
		branch="branch-smoke",
		raw={"attendee_ref": "mem-1", "client_req_id": "b:mem-1", "member": "mem-1"},
		index=0,
	)
	assert item.gathering == "gathering-1"
	assert item.member == "mem-1"
	assert item.event == "gathering-1"  # gathering is the grouping axis


def test_item_from_payload_stamps_gathering_null_member_for_visitor(fake_frappe) -> None:
	"""A visitor row (no ``member`` field) stamps ``gathering=event``, ``member=None``."""
	item = attendance._item_from_payload(
		event="gathering-1",
		branch="branch-smoke",
		raw={"attendee_ref": "visitor-xyz", "client_req_id": "b:visitor-xyz"},
		index=0,
	)
	assert item.gathering == "gathering-1"
	assert item.member is None  # no dangling visitor ref in the member FK


def test_bulk_submit_carries_provenance_through_enqueue(fake_frappe, monkeypatch) -> None:
	"""The RQ payload carries ``gathering`` + ``member`` across the enqueue boundary."""
	fake_scope = attendance.AttendanceScope(branch="branch-smoke")
	monkeypatch.setattr(attendance, "_resolve_caller_branch_scope", lambda: fake_scope)

	items = [
		{"attendee_ref": "mem-1", "client_req_id": "b:mem-1", "member": "mem-1"},
		{"attendee_ref": "visitor-2", "client_req_id": "b:visitor-2"},
	]
	attendance.bulk_submit(event="gathering-1", items=items, batch_id="batch-prov")

	call = fake_frappe.enqueue_calls[0]
	enqueued = call["payload"]["items"]
	assert enqueued[0]["gathering"] == "gathering-1"
	assert enqueued[0]["member"] == "mem-1"
	assert enqueued[1]["gathering"] == "gathering-1"
	assert enqueued[1]["member"] is None


def test_process_bulk_batch_reconstructs_provenance(fake_frappe, monkeypatch) -> None:
	"""The RQ job reconstructs ``gathering`` + ``member`` from the payload."""

	captured: list[Any] = []

	class _CapturingService:
		def __init__(self, _gateway: Any) -> None:
			pass

		def submit(self, items: Any, _scope: Any, _batch_id: Any) -> Any:
			captured.extend(items)
			return types.SimpleNamespace(accepted=True, inserted=len(items), deduplicated=0, rejected_count=0)

	monkeypatch.setattr(attendance, "BulkAttendanceService", _CapturingService)
	monkeypatch.setattr(attendance, "_retryable_persistence_errors", lambda: ())

	payload = {
		"event": "gathering-1",
		"batch_id": "batch-rebuild",
		"scope_branch": "branch-smoke",
		"items": [
			{
				"event": "gathering-1",
				"attendee_ref": "mem-1",
				"branch": "branch-smoke",
				"status": "Present",
				"source": "bulk",
				"client_req_id": "b:mem-1",
				"gathering": "gathering-1",
				"member": "mem-1",
			},
			{
				"event": "gathering-1",
				"attendee_ref": "visitor-2",
				"branch": "branch-smoke",
				"status": "Present",
				"source": "bulk",
				"client_req_id": "b:visitor-2",
				"gathering": "gathering-1",
				"member": None,
			},
		],
	}

	attendance.process_bulk_batch(payload)

	assert len(captured) == 2
	assert captured[0].gathering == "gathering-1"
	assert captured[0].member == "mem-1"
	assert captured[1].gathering == "gathering-1"
	assert captured[1].member is None


def test_manual_and_engagement_credits_for_same_member_dedup() -> None:
	"""ADR §9 / FLO-199: a manual-roster credit + an engagement credit for the
	same ``(branch, gathering, member)`` collapse to one attendance row.

	Both paths now stamp ``event=gathering, attendee_ref=member_id, gathering,
	member`` for members, so the service-level ``(event, attendee_ref)`` unique
	backstop dedupes the second credit — exactly what the DB-level
	``UNIQUE (branch, gathering, member)`` index enforces on the real schema.
	"""
	from flock_os.engagement import Participation, _participation_to_attendance_item
	from flock_os.reporting import (
		AttendanceItem,
		AttendanceScope,
		BulkAttendanceService,
		InMemoryBulkAttendanceGateway,
	)

	gateway = InMemoryBulkAttendanceGateway()
	service = BulkAttendanceService(gateway)
	scope = AttendanceScope(branch="branch-a")
	gathering = "gathering-1"
	member = "mem-dedup"

	# 1. Manual-roster credit: leader takes roll for the member.
	manual_item = AttendanceItem(
		event=gathering,
		attendee_ref=member,
		branch="branch-a",
		status="Present",
		source="bulk",
		client_req_id=f"manual:{member}",
		gathering=gathering,
		member=member,
	)
	first = service.submit([manual_item], scope, batch_id="manual-batch")
	assert first.inserted == 1

	# 2. Engagement credit: same member plays a game at the same gathering.
	#    The engagement projection stamps the identical (gathering, member).
	engagement_item = _participation_to_attendance_item(
		Participation(
			session_id="sess-1",
			attendee_key="key-dedup",
			member_id=member,
			attendee_display_name="Dedup Member",
			device_fingerprint="dev",
			role="member",
			engagement_type="game",
			engagement_kind="tap_burst",
			score=80.0,
			submitted_at=1000.0,
			client_submitted_at=None,
			branch="branch-a",
			organization="org-1",
			group=None,
			geo_region=None,
			nonce="n",
			gathering=gathering,
		)
	)
	# The two items share the cross-source provenance keys.
	assert engagement_item.gathering == gathering
	assert engagement_item.member == member
	assert engagement_item.event == gathering

	replay = service.submit([engagement_item], scope, batch_id="engagement-batch")

	# The second credit is deduplicated — no double-counting.
	assert replay.inserted == 0
	assert replay.deduplicated == 1
	# The maintained aggregate counts the member exactly once.
	assert service.aggregate(scope, event=gathering) == 1
	# The single persisted row carries the provenance from the first writer.
	assert gateway.inserted_items[0].gathering == gathering
	assert gateway.inserted_items[0].member == member
