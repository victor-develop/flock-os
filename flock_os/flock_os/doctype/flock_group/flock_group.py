# Copyright (c) 2026, Flock OS and contributors
# For license information, please see license.txt

from __future__ import annotations

import frappe
from frappe.model.document import Document

from flock_os.flock_os import trees


class FlockGroup(Document):
	# Flock Group = the ministry/cell tree (is_tree=1), branch-bound (FLO-5 §3.3).
	# Leadership: single accountable leader on `leader`; the full roster lives on
	# Flock Group Member (role=Leader/Co-Leader). Structural moves go through
	# flock_os.trees.move_group (FLO-19/20).

	def validate(self):
		_branch_organization_default(self)
		self._validate_branch_binding()

	def _validate_branch_binding(self):
		# ADR §4.2 / FLO-5 §3.3: a child group must inherit its parent's branch.
		# The group subtree is branch-bound; branch is immutable within it.
		parent_branch = None
		if self.parent_group:
			parent_branch = frappe.db.get_value("Flock Group", self.parent_group, "branch")
		try:
			trees.validate_group_branch_binding(parent_branch=parent_branch, child_branch=self.branch)
		except trees.FlockTreeError as exc:
			frappe.throw(str(exc), frappe.ValidationError)


def _branch_organization_default(doc: Document) -> None:
	# Cheap denormalized tenant floor (ADR §3 contract): a group's organization
	# mirrors its branch's organization so the whole group subtree shares one org.
	if doc.organization or not doc.branch:
		return
	branch_org = frappe.db.get_value("Flock Branch", doc.branch, "organization")
	if branch_org:
		doc.organization = branch_org
