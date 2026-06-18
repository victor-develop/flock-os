# Copyright (c) 2026, Flock OS and Contributors
# License: MIT. Frappe-level integration tests for Flock Announcement — run via
# `bench run-tests`. Project-level (pytest, no site) scope + audience tests live
# in flock_os/tests/test_scheduling.py.

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from flock_os import scheduling


def _ensure(name: str, doctype: str, **fields) -> str:
	if frappe.db.exists(doctype, name):
		return name
	doc = frappe.get_doc({"doctype": doctype, **fields})
	doc.insert(ignore_permissions=True)
	return doc.name


class TestFlockAnnouncement(FrappeTestCase):
	"""Flock Announcement CRUD + scope validation (FLO-8 §3, ADR §4/§6.1)."""

	def setUp(self):
		self.org = _ensure("Test Org", "Flock Organization", organization_name="Test Org")
		self.branch_north = _ensure(
			"Test Branch North", "Flock Branch", branch_name="Test Branch North", organization=self.org
		)
		self.branch_south = _ensure(
			"Test Branch South", "Flock Branch", branch_name="Test Branch South", organization=self.org
		)
		self.group_south = _ensure(
			"Test Branch South-Test Group South",
			"Flock Group",
			group_name="Test Group South",
			branch=self.branch_south,
		)
		frappe.db.delete("Flock Announcement", {"organization": self.org})

	def tearDown(self):
		frappe.db.delete("Flock Announcement", {"organization": self.org})

	def test_draft_announcement_inserts(self):
		doc = frappe.get_doc(
			{
				"doctype": "Flock Announcement",
				"subject": "Sunday Service",
				"body": "Welcome everyone.",
				"organization": self.org,
				"branch": self.branch_north,
			}
		)
		doc.insert(ignore_permissions=True)
		self.assertEqual(doc.status, "Draft")
		self.assertTrue(doc.name)

	def test_organization_denormalized_from_branch(self):
		# ADR §3 contract: organization mirrors the branch's org when unset.
		doc = frappe.get_doc(
			{
				"doctype": "Flock Announcement",
				"subject": "No Org Set",
				"body": "Body.",
				"branch": self.branch_north,
			}
		)
		doc.insert(ignore_permissions=True)
		self.assertEqual(doc.organization, self.org)

	def test_group_branch_binding_mismatch_rejected(self):
		# ADR §4: a North-scoped announcement may not target a South-bound group.
		doc = frappe.get_doc(
			{
				"doctype": "Flock Announcement",
				"subject": "Cross-branch",
				"body": "Body.",
				"organization": self.org,
				"branch": self.branch_north,
				"group": self.group_south,
			}
		)
		self.assertRaises(frappe.ValidationError, doc.insert, ignore_permissions=True)

	def test_group_scoped_announcement_inserts(self):
		# A South announcement targeting a South-bound group is valid.
		doc = frappe.get_doc(
			{
				"doctype": "Flock Announcement",
				"subject": "South group",
				"body": "Body.",
				"organization": self.org,
				"branch": self.branch_south,
				"group": self.group_south,
			}
		)
		doc.insert(ignore_permissions=True)
		self.assertEqual(doc.group, self.group_south)

	def test_scheduled_requires_send_at(self):
		doc = frappe.get_doc(
			{
				"doctype": "Flock Announcement",
				"subject": "Scheduled no time",
				"body": "Body.",
				"organization": self.org,
				"branch": self.branch_north,
				"status": "Scheduled",
			}
		)
		self.assertRaises(frappe.ValidationError, doc.insert, ignore_permissions=True)

	def test_production_gateway_resolves_audience_subtree(self):
		# The Frappe adapter + resolve_audience_branches return the branch subtree.
		scheduling.install_gateway(scheduling.FrappeSchedulingGateway())
		branches = scheduling.resolve_audience_branches(self.branch_north, gateway=scheduling.get_gateway())
		self.assertIn(self.branch_north, branches)
