# Copyright (c) 2026, Flock OS and Contributors
# License: MIT. Frappe-level integration tests for Flock Gathering — run via
# `bench run-tests`. Project-level (pytest, no site) schema + pure-domain tests
# live in flock_os/tests/.

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase


def _make_branch(name: str, org: str) -> str:
	if frappe.db.exists("Flock Branch", name):
		return name
	frappe.get_doc({"doctype": "Flock Branch", "branch_name": name, "organization": org}).insert(
		ignore_permissions=True
	)
	return name


def _make_group(name: str, branch: str, parent: str | None = None) -> str:
	if frappe.db.exists("Flock Group", f"{branch}-{name}"):
		return f"{branch}-{name}"
	doc = frappe.get_doc(
		{"doctype": "Flock Group", "group_name": name, "branch": branch, "parent_group": parent}
	)
	doc.insert(ignore_permissions=True)
	return doc.name


class TestFlockGathering(FrappeTestCase):
	"""Flock Gathering scoping contract + reporting state machine (FLO-6 §3.2/§4)."""

	def setUp(self):
		self.org = "Test Org"
		if not frappe.db.exists("Flock Organization", self.org):
			frappe.get_doc({"doctype": "Flock Organization", "organization_name": self.org}).insert(
				ignore_permissions=True
			)
		self.branch_a = _make_branch("Gather Branch A", self.org)
		self.branch_b = _make_branch("Gather Branch B", self.org)
		self.group_a = _make_group("Worship", self.branch_a)
		self.group_a_child = _make_group("Band", self.branch_a, parent=self.group_a)
		frappe.db.delete("Flock Gathering", {"branch": ("in", (self.branch_a, self.branch_b))})

	def tearDown(self):
		# Clean the report-workflow artifacts (FLO-56) before the gatherings they
		# reference are removed, so no orphaned attendance/summary/member rows
		# leak across runs (member uniqueness is (email, branch) — FLO-5 §8.3).
		frappe.db.delete("Flock Attendance Record", {"branch": ("in", (self.branch_a, self.branch_b))})
		frappe.db.delete("Event Attendance Summary", {"branch": ("in", (self.branch_a, self.branch_b))})
		frappe.db.delete("Flock Member", {"branch": ("in", (self.branch_a, self.branch_b))})
		frappe.db.delete("Flock Gathering", {"branch": ("in", (self.branch_a, self.branch_b))})

	def test_create_gathering_denormalizes_org_and_group_path(self):
		# A new gathering defaults to Scheduled, inherits the group's branch's
		# organization, and denormalizes a root-first group_path.
		doc = frappe.get_doc(
			{
				"doctype": "Flock Gathering",
				"title": "Sunday Service",
				"branch": self.branch_a,
				"group": self.group_a_child,
				"starts_on": "2026-06-21 10:00:00",
			}
		)
		doc.insert(ignore_permissions=True)
		self.assertEqual(doc.status, "Scheduled")
		self.assertEqual(doc.organization, self.org)
		# Root-first path includes both the child and its parent group.
		self.assertIn(self.group_a, doc.group_path)
		self.assertIn(self.group_a_child, doc.group_path)
		self.assertTrue(doc.group_path.startswith("/"))

	def test_gathering_branch_must_match_group_branch(self):
		# ADR §4.2: the gathering is branch-bound to its group.
		doc = frappe.get_doc(
			{
				"doctype": "Flock Gathering",
				"title": "Cross Branch",
				"branch": self.branch_b,  # diverges from group_a's branch
				"group": self.group_a,
				"starts_on": "2026-06-21 10:00:00",
			}
		)
		self.assertRaises(frappe.ValidationError, doc.insert, ignore_permissions=True)

	def test_status_transitions_guard(self):
		# FLO-6 §4: legal forward transition Scheduled -> Held is allowed.
		doc = frappe.get_doc(
			{
				"doctype": "Flock Gathering",
				"title": "Youth",
				"branch": self.branch_a,
				"group": self.group_a,
				"starts_on": "2026-06-21 10:00:00",
			}
		)
		doc.insert(ignore_permissions=True)
		doc.status = "Held"
		doc.save(ignore_permissions=True)
		self.assertEqual(doc.status, "Held")
		# Illegal jump Held -> Confirmed (must go via Reported) is rejected.
		doc.status = "Confirmed"
		self.assertRaises(frappe.ValidationError, doc.save, ignore_permissions=True)

	# ------------------------------------------------------------------ #
	# Leader attendance-report workflow (FLO-56 / FLO-6 §4)
	# ------------------------------------------------------------------ #
	def _make_member(self, first_name: str, status: str, email: str) -> str:
		doc = frappe.get_doc(
			{
				"doctype": "Flock Member",
				"first_name": first_name,
				"branch": self.branch_a,
				"email": email,
				"status": status,
			}
		)
		doc.insert(ignore_permissions=True)
		return doc.name

	def test_leader_report_workflow_records_attendance_and_advances(self):
		# FLO-56: a leader report records members + visitors/pre-members through
		# the canonical bulk path, drives Held -> Reported, stamps counters, and
		# emits flock.attendance.reported through the single sanctioned emitter.
		from flock_os.events import ATTENDANCE_REPORTED
		from flock_os.leader_reporting import (
			AttendeeReport,
			FrappeLeaderReportingGateway,
			LeaderReportingService,
			ReportSubmission,
		)

		gathering = frappe.get_doc(
			{
				"doctype": "Flock Gathering",
				"title": "Sunday Report",
				"branch": self.branch_a,
				"group": self.group_a,
				"starts_on": "2026-06-21 10:00:00",
				"status": "Held",
			}
		)
		gathering.insert(ignore_permissions=True)

		member = self._make_member("Grace", "Member", "grace-r@example.org")
		visitor = self._make_member("Hope", "Visitor", "hope-r@example.org")
		premember = self._make_member("Joy", "Pre-Member", "joy-r@example.org")

		outcome = LeaderReportingService(FrappeLeaderReportingGateway()).submit_report(
			ReportSubmission(
				gathering=gathering.name,
				branch=self.branch_a,
				group=self.group_a,
				reported_by=member,
				attendees=[
					AttendeeReport(member=member),
					AttendeeReport(member=visitor),
					AttendeeReport(member=premember, first_time=True),
				],
				client_batch_id="bench-report-1",
			)
		)

		# Three attendees recorded through the bulk write path (one row each).
		self.assertTrue(outcome.accepted)
		self.assertEqual(outcome.inserted, 3)
		self.assertEqual(outcome.member_count, 1)
		self.assertEqual(outcome.visitor_count, 2)  # Visitor + Pre-Member
		self.assertEqual(outcome.first_time_count, 1)
		# Attendance rows landed in the canonical attendance DocType.
		rows = frappe.db.get_all(
			"Flock Attendance Record",
			filters={"event": gathering.name},
			pluck="attendee_ref",
		)
		self.assertEqual(sorted(rows), sorted([member, visitor, premember]))
		# Gathering advanced Held -> Reported with stamped roll-up counters.
		gathering.reload()
		self.assertEqual(gathering.status, "Reported")
		self.assertEqual(gathering.reported_by, member)
		self.assertEqual(gathering.member_attendance_count, 1)
		self.assertEqual(gathering.visitor_attendance_count, 2)
		self.assertEqual(gathering.total_attendance_count, 3)
		self.assertEqual(gathering.first_time_count, 1)
		# The emitted event name is the canonical one (no dual emitter).
		self.assertEqual(ATTENDANCE_REPORTED, "flock.attendance.reported")
