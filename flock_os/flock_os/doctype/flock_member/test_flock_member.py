# Copyright (c) 2026, Flock OS and Contributors
# License: MIT. Frappe-level integration tests for Flock Member — run via
# `bench run-tests`.

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase


def _ensure_branch(name: str, org: str) -> str:
	if frappe.db.exists("Flock Branch", name):
		return name
	frappe.get_doc({"doctype": "Flock Branch", "branch_name": name, "organization": org}).insert(
		ignore_permissions=True
	)
	return name


class TestFlockMember(FrappeTestCase):
	"""Flock Member full_name + (email, branch) uniqueness (FLO-5 §3.2/§8.3)."""

	def setUp(self):
		self.org = "Member Test Org"
		if not frappe.db.exists("Flock Organization", self.org):
			frappe.get_doc({"doctype": "Flock Organization", "organization_name": self.org}).insert(
				ignore_permissions=True
			)
		self.branch_a = _ensure_branch("Member Branch A", self.org)
		self.branch_b = _ensure_branch("Member Branch B", self.org)
		frappe.db.delete("Flock Member", {"branch": ("in", (self.branch_a, self.branch_b))})

	def tearDown(self):
		frappe.db.delete("Flock Member", {"branch": ("in", (self.branch_a, self.branch_b))})

	def test_full_name_is_derived(self):
		m = frappe.get_doc(
			{
				"doctype": "Flock Member",
				"first_name": "Grace",
				"last_name": "Lee",
				"branch": self.branch_a,
				"email": "grace-a@example.org",
			}
		)
		m.insert(ignore_permissions=True)
		self.assertEqual(m.full_name, "Grace Lee")

	def test_email_branch_uniqueness_within_branch(self):
		# Same email + same branch is rejected.
		payload = {
			"doctype": "Flock Member",
			"first_name": "Sam",
			"branch": self.branch_a,
			"email": "sam-dup@example.org",
		}
		frappe.get_doc(payload).insert(ignore_permissions=True)
		self.assertRaises(
			frappe.ValidationError,
			frappe.get_doc(payload).insert,
			ignore_permissions=True,
		)

	def test_same_email_allowed_across_branches(self):
		# FLO-5 §8.3: the same email may exist at two campuses (separate rows).
		payload_a = {
			"doctype": "Flock Member",
			"first_name": "Sam",
			"branch": self.branch_a,
			"email": "sam-cross@example.org",
		}
		payload_b = dict(payload_a, branch=self.branch_b)
		frappe.get_doc(payload_a).insert(ignore_permissions=True)
		frappe.get_doc(payload_b).insert(ignore_permissions=True)
		self.assertEqual(
			frappe.db.count("Flock Member", {"email": "sam-cross@example.org"}),
			2,
		)

	def test_status_must_be_canonical(self):
		m = frappe.get_doc(
			{
				"doctype": "Flock Member",
				"first_name": "Pat",
				"branch": self.branch_a,
				"status": "Bogus",
			}
		)
		self.assertRaises(frappe.ValidationError, m.insert, ignore_permissions=True)
