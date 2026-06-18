# Copyright (c) 2026, Flock OS and Contributors
# License: MIT. Frappe-level integration tests for Flock Gathering Type — run
# via `bench run-tests`. Project-level schema tests live in flock_os/tests/.

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase


class TestFlockGatheringType(FrappeTestCase):
	"""Flock Gathering Type config master (FLO-6 §3.1)."""

	def test_gathering_type_is_unique_by_name(self):
		# autoname: field:gathering_type_name + unique → a duplicate name raises.
		name = "Unique Service Type"
		frappe.get_doc({"doctype": "Flock Gathering Type", "gathering_type_name": name}).insert(
			ignore_permissions=True
		)
		self.addCleanup(lambda: frappe.delete_doc("Flock Gathering Type", name))
		dup = frappe.get_doc({"doctype": "Flock Gathering Type", "gathering_type_name": name})
		self.assertRaises(frappe.ValidationError, dup.insert, ignore_permissions=True)

	def test_gathering_type_branch_is_optional(self):
		# §3.1: branch is nullable (org-wide default). A type with no branch saves.
		name = "Org-Wide Study"
		doc = frappe.get_doc({"doctype": "Flock Gathering Type", "gathering_type_name": name})
		doc.insert(ignore_permissions=True)
		self.addCleanup(lambda: frappe.delete_doc("Flock Gathering Type", name))
		self.assertIsNone(doc.branch)
