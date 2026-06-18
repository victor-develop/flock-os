# Copyright (c) 2026, Flock OS and Contributors
# License: MIT. Frappe-level integration tests for Flock Attendance Record and
# the FrappeBulkAttendanceGateway round-trip (FLO-64 DoD) — run via
# `bench run-tests`. The project-level (pytest, no-site) schema contract lives
# in flock_os/tests/test_doctype_schema.py.

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from flock_os.flock_os.patches.v0_1.add_attendance_indexes import INDEXES
from flock_os.flock_os.patches.v0_1.add_attendance_indexes import execute as run_index_patch
from flock_os.reporting import (
	AttendanceItem,
	AttendanceScope,
	FrappeBulkAttendanceGateway,
)


def _ensure_branch(name: str, org: str) -> str:
	if frappe.db.exists("Flock Branch", name):
		return name
	frappe.get_doc({"doctype": "Flock Branch", "branch_name": name, "organization": org}).insert(
		ignore_permissions=True
	)
	return name


class TestFlockAttendanceRecord(FrappeTestCase):
	"""Raw bulk-insert write path + the composite UNIQUE indexes (FLO-64)."""

	def setUp(self):
		self.org = "Attendance Test Org"
		if not frappe.db.exists("Flock Organization", self.org):
			frappe.get_doc({"doctype": "Flock Organization", "organization_name": self.org}).insert(
				ignore_permissions=True
			)
		self.branch_a = _ensure_branch("Attendance Branch A", self.org)
		self.branch_b = _ensure_branch("Attendance Branch B", self.org)
		frappe.db.delete("Flock Attendance Record")
		frappe.db.delete("Event Attendance Summary")
		# The composite indexes are created by the migrate patch; make sure they
		# exist on the test site (idempotent) so the dedupe backstop is live.
		run_index_patch()

	def tearDown(self):
		frappe.db.delete("Flock Attendance Record")
		frappe.db.delete("Event Attendance Summary")

	def test_composite_unique_indexes_present(self):
		for doctype, index_name, _columns in INDEXES:
			self.assertTrue(
				frappe.db.sql(
					"""SELECT 1 FROM information_schema.STATISTICS
					WHERE table_schema = DATABASE() AND table_name = %s
					AND index_name = %s LIMIT 1""",
					(f"tab{doctype}", index_name),
				),
				f"missing UNIQUE index {index_name} on tab{doctype}",
			)

	def test_bulk_insert_without_name_auto_generates_rows(self):
		# Raw bulk insert omits `name`; auto-increment must fill it.
		frappe.db.bulk_insert(
			"Flock Attendance Record",
			fields=["event", "attendee_ref", "branch", "status", "source", "client_req_id"],
			values=[
				["g-1", "m-1", self.branch_a, "present", "bulk", "b1:m-1"],
				["g-1", "m-2", self.branch_a, "present", "bulk", "b1:m-2"],
			],
		)
		self.assertEqual(frappe.db.count("Flock Attendance Record", {"event": "g-1"}), 2)

	def test_unique_event_attendee_backstop_rejects_double_count(self):
		frappe.db.bulk_insert(
			"Flock Attendance Record",
			fields=["event", "attendee_ref", "branch", "status", "source", "client_req_id"],
			values=[["g-1", "m-1", self.branch_a, "present", "bulk", "b1:m-1"]],
		)
		# Same (event, attendee_ref), different client_req_id — the
		# UNIQUE (event, attendee_ref) backstop must reject the double-count.
		# (Raw INSERT → IntegrityError-class; assert the class-agnostic contract.)
		raised = False
		try:
			frappe.db.bulk_insert(
				"Flock Attendance Record",
				fields=["event", "attendee_ref", "branch", "status", "source", "client_req_id"],
				values=[["g-1", "m-1", self.branch_a, "present", "bulk", "b2:m-1"]],
			)
		except Exception:
			raised = True
		self.assertTrue(raised, "duplicate (event, attendee_ref) was not rejected by the backstop")
		self.assertEqual(frappe.db.count("Flock Attendance Record", {"event": "g-1"}), 1)

	def test_summary_upsert_is_atomic(self):
		# ON DUPLICATE KEY UPDATE depends on the UNIQUE (branch, event) index.
		frappe.db.sql(
			"""INSERT INTO `tabEvent Attendance Summary` (branch, event, total)
			VALUES (%s, %s, %s)
			ON DUPLICATE KEY UPDATE total = total + VALUES(total)""",
			(self.branch_a, "g-1", 10),
		)
		frappe.db.sql(
			"""INSERT INTO `tabEvent Attendance Summary` (branch, event, total)
			VALUES (%s, %s, %s)
			ON DUPLICATE KEY UPDATE total = total + VALUES(total)""",
			(self.branch_a, "g-1", 5),
		)
		total = frappe.db.get_value(
			"Event Attendance Summary", {"branch": self.branch_a, "event": "g-1"}, "total"
		)
		self.assertEqual(int(total), 15)


class TestFrappeBulkAttendanceGatewayRoundTrip(FrappeTestCase):
	"""FLO-64 DoD: the production gateway round-trips end-to-end on a migrated
	site (bulk_insert → filter_unseen → increment_aggregate → aggregate)."""

	def setUp(self):
		self.org = "Gateway Test Org"
		if not frappe.db.exists("Flock Organization", self.org):
			frappe.get_doc({"doctype": "Flock Organization", "organization_name": self.org}).insert(
				ignore_permissions=True
			)
		self.branch = _ensure_branch("Gateway Branch", self.org)
		frappe.db.delete("Flock Attendance Record")
		frappe.db.delete("Event Attendance Summary")
		run_index_patch()
		self.gateway = FrappeBulkAttendanceGateway()

	def tearDown(self):
		frappe.db.delete("Flock Attendance Record")
		frappe.db.delete("Event Attendance Summary")

	def _items(self, n, *, event="g-1", prefix="b1", start=0):
		return [
			AttendanceItem(
				event=event,
				attendee_ref=f"m-{start + i}",
				branch=self.branch,
				client_req_id=f"{prefix}:m-{start + i}",
			)
			for i in range(n)
		]

	def test_bulk_insert_filter_unseen_aggregate_round_trip(self):
		scope = AttendanceScope(self.branch)
		items = self._items(10)

		# Nothing seen yet — all keys are unseen.
		keys = [item.idempotency_key for item in items]
		self.assertEqual(len(self.gateway.filter_unseen(keys)), 10)

		# Bulk insert raw, then bump the maintained aggregate.
		self.assertEqual(self.gateway.bulk_insert(items), 10)
		self.gateway.increment_aggregate(scope, "g-1", 10)
		self.assertEqual(self.gateway.aggregate(scope, "g-1"), 10)
		self.assertEqual(self.gateway.aggregate(scope), 10)

		# Replay: all keys are now seen → filter_unseen returns the empty set.
		self.assertEqual(self.gateway.filter_unseen(keys), set())

	def test_partial_overlap_dedupes_and_increments_only_new(self):
		scope = AttendanceScope(self.branch)
		first = self._items(10, prefix="b1")  # members 0-9
		self.gateway.bulk_insert(first)
		self.gateway.increment_aggregate(scope, "g-1", 10)

		overlap = self._items(10, prefix="b2", start=5)  # members 5-14
		keys = [item.idempotency_key for item in overlap]
		unseen = self.gateway.filter_unseen(keys)
		new_items = [item for item in overlap if item.idempotency_key in unseen]
		self.assertEqual(len(new_items), 5)  # members 10-14

		self.gateway.bulk_insert(new_items)
		self.gateway.increment_aggregate(scope, "g-1", 5)
		self.assertEqual(self.gateway.aggregate(scope, "g-1"), 15)
