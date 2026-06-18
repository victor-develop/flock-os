# Copyright (c) 2026, Flock OS and contributors
# For license information, please see license.txt

from __future__ import annotations

import frappe
from frappe.model.document import Document

from flock_os.flock_os import rules


class FlockGroupMember(Document):
	# Flock Group Member = the M:N membership edge (FLO-5 §3.3). `branch` is
	# denormalized from group.branch (Architect rec. #5) so the edge gets native
	# branch isolation + the group-axis hook. Unique on (group, member). The
	# leadership roster (role=Leader/Co-Leader) is the multi-leader reality;
	# the single accountable leader lives on Flock Group.leader.

	def validate(self):
		rules.validate_group_member_role(self.role)
		rules.validate_group_member_status(self.status)
		self._denormalize_branch_and_org()
		self._validate_group_member_uniqueness()

	def _denormalize_branch_and_org(self):
		# Branch is immutable within a group subtree (flock_os.trees), so the
		# edge's branch is derived from the group, not set by the caller.
		group_values = frappe.db.get_value(
			"Flock Group", self.group, ("branch", "organization"), as_dict=True
		)
		if not group_values:
			frappe.throw(f"Linked group {self.group!r} does not exist.", frappe.ValidationError)
		group_branch = group_values.branch
		self.branch = rules.denormalize_group_member_branch(group_branch=group_branch)
		if group_values.organization:
			self.organization = group_values.organization
		rules.validate_group_member_branch_matches(member_branch=self.branch, group_branch=group_branch)

	def _validate_group_member_uniqueness(self):
		# (group, member) composite uniqueness (FLO-5 §3.3).
		existing = frappe.db.get_all(
			"Flock Group Member",
			filters={"group": self.group, "member": self.member, "name": ["!=", self.name or ""]},
			pluck="name",
		)
		pairs = [(self.group, self.member)] * len(existing)
		if rules.is_duplicate_pair((self.group, self.member), pairs):
			frappe.throw(
				f"Member {self.member!r} is already a member of group {self.group!r}.",
				frappe.ValidationError,
			)
