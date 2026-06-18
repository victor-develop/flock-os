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
