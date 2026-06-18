# Copyright (c) 2026, Flock OS and Contributors
# License: MIT. Frappe-level integration tests for Flock Group Member — run via
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


def _ensure_group(name: str, branch: str) -> str:
	if frappe.db.exists("Flock Group", f"{branch}-{name}"):
		return f"{branch}-{name}"
	doc = frappe.get_doc({"doctype": "Flock Group", "group_name": name, "branch": branch})
	doc.insert(ignore_permissions=True)
	return doc.name


def _ensure_member(first: str, branch: str, email: str) -> str:
	doc = frappe.get_doc({"doctype": "Flock Member", "first_name": first, "branch": branch, "email": email})
	doc.insert(ignore_permissions=True)
	return doc.name


class TestFlockGroupMember(FrappeTestCase):
	"""Edge denormalization + (group, member) uniqueness (FLO-5 §3.3, rec #5)."""

	def setUp(self):
		self.org = "Edge Test Org"
		if not frappe.db.exists("Flock Organization", self.org):
			frappe.get_doc({"doctype": "Flock Organization", "organization_name": self.org}).insert(
				ignore_permissions=True
			)
		self.branch = _ensure_branch("Edge Branch", self.org)
		self.group = _ensure_group("Worship", self.branch)
		self.member = _ensure_member("Ivy", self.branch, "ivy-edge@example.org")
		frappe.db.delete("Flock Group Member", {"group": self.group})

	def tearDown(self):
		frappe.db.delete("Flock Group Member", {"group": self.group})

	def test_branch_denormalized_from_group(self):
		edge = frappe.get_doc(
			{
				"doctype": "Flock Group Member",
				"group": self.group,
				"member": self.member,
			}
		)
		edge.insert(ignore_permissions=True)
		self.assertEqual(edge.branch, self.branch)

	def test_group_member_uniqueness(self):
		payload = {"doctype": "Flock Group Member", "group": self.group, "member": self.member}
		frappe.get_doc(payload).insert(ignore_permissions=True)
		self.assertRaises(
			frappe.ValidationError,
			frappe.get_doc(payload).insert,
			ignore_permissions=True,
		)

	def test_role_must_be_canonical(self):
		edge = frappe.get_doc(
			{
				"doctype": "Flock Group Member",
				"group": self.group,
				"member": self.member,
				"role": "Captain",
			}
		)
		self.assertRaises(frappe.ValidationError, edge.insert, ignore_permissions=True)
