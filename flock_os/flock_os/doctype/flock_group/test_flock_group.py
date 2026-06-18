# Copyright (c) 2026, Flock OS and Contributors
# License: MIT. Frappe-level integration tests for Flock Group — run via
# `bench run-tests`. Project-level (pytest, no site) schema + rule tests live in
# flock_os/tests/.

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase


def _make_branch(name: str, org: str) -> str:
	if frappe.db.exists("Flock Branch", name):
		return name
	doc = frappe.get_doc({"doctype": "Flock Branch", "branch_name": name, "organization": org})
	doc.insert(ignore_permissions=True)
	return doc.name


class TestFlockGroup(FrappeTestCase):
	"""Flock Group tree + branch-binding invariants (ADR §4.2, FLO-5 §3.3)."""

	def setUp(self):
		self.org = "Test Org"
		if not frappe.db.exists("Flock Organization", self.org):
			frappe.get_doc({"doctype": "Flock Organization", "organization_name": self.org}).insert(
				ignore_permissions=True
			)
		self.branch_a = _make_branch("Test Branch A", self.org)
		self.branch_b = _make_branch("Test Branch B", self.org)
		frappe.db.delete("Flock Group", {"branch": ("in", (self.branch_a, self.branch_b))})

	def tearDown(self):
		frappe.db.delete("Flock Group", {"branch": ("in", (self.branch_a, self.branch_b))})

	def test_root_group_sets_branch(self):
		# A root group (no parent) sets the branch for its subtree.
		group = frappe.get_doc(
			{
				"doctype": "Flock Group",
				"group_name": "Worship Team",
				"branch": self.branch_a,
			}
		)
		group.insert(ignore_permissions=True)
		self.assertEqual(group.branch, self.branch_a)
		self.assertTrue(group.name)

	def test_child_group_must_inherit_parent_branch(self):
		# FLO-5 §3.3: a child group's branch must equal its parent's branch.
		parent = frappe.get_doc({"doctype": "Flock Group", "group_name": "Parent", "branch": self.branch_a})
		parent.insert(ignore_permissions=True)

		child = frappe.get_doc(
			{
				"doctype": "Flock Group",
				"group_name": "Child",
				"branch": self.branch_b,  # diverges from parent's branch
				"parent_group": parent.name,
			}
		)
		self.assertRaises(frappe.ValidationError, child.insert, ignore_permissions=True)

	def test_child_group_inheriting_correct_branch_inserts(self):
		parent = frappe.get_doc(
			{"doctype": "Flock Group", "group_name": "Parent OK", "branch": self.branch_a}
		)
		parent.insert(ignore_permissions=True)
		child = frappe.get_doc(
			{
				"doctype": "Flock Group",
				"group_name": "Child OK",
				"branch": self.branch_a,
				"parent_group": parent.name,
			}
		)
		child.insert(ignore_permissions=True)
		self.assertEqual(child.parent_group, parent.name)
