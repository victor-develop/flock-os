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

	def reset(self) -> None:
		self.enqueue_calls.clear()

	def enqueue(self, method: str, **kwargs: Any) -> None:
		self.enqueue_calls.append({"method": method, **kwargs})

	def throw(self, msg: str, **_kwargs: Any) -> None:
		raise ValueError(msg)


class _FakePermissions:
	def get_user_permissions(self, _user: str) -> dict[str, list[dict[str, Any]]]:
		return {"Flock Branch": [{"doc": "branch-smoke"}]}


_FRAPPE_STUB = _FakeFrappe()
sys.modules.setdefault("frappe", _FRAPPE_STUB)

from flock_os import attendance  # noqa: E402
from flock_os.reporting import BULK_ATTENDANCE_JOB_QUEUE  # noqa: E402

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
