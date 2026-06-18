# Copyright (c) 2026, Flock OS and contributors
# For license information, please see license.txt

from __future__ import annotations

import frappe
from frappe.model.document import Document

from flock_os import permissions


class FlockBranchAdminScope(Document):
	# Drives the native branch-axis User-Permission subtree sync (ADR §6.2 /
	# FLO-5 §4.2). One row per (user, root branch) admin assignment: the branch
	# admin's allowed set = that branch + its descendant branches, materialized
	# as User Permission(Branch=<n>) rows on validate by
	# flock_os.permissions.FrappeUserPermissionSyncer. Re-syncs on branch-tree
	# moves via the flock.branch.moved subscriber wired in hooks.py.

	def validate(self):
		self._sync_user_permissions()

	def on_trash(self):
		self._withdraw_user_permissions()

	def _resolve_subtree(self) -> list[str]:
		# The allowed set = the configured branch + its descendant branches. The
		# subtree is computed live from the current nested set (ADR §6.2) so a
		# move is reflected on the next sync without a stale cache.
		if not self.branch:
			return []
		rows = frappe.get_all(
			"Flock Branch",
			filters={"lft": [">=", frappe.db.get_value("Flock Branch", self.branch, "lft")]},
			pluck="name",
		)
		return list(rows)

	def _sync_user_permissions(self):
		syncer = permissions.FrappeUserPermissionSyncer()
		syncer.sync_branch_scope(
			user=self.user,
			branch_subtree=self._resolve_subtree(),
			organization=self.organization,
		)

	def _withdraw_user_permissions(self):
		# Drop the admin's Flock-Branch User Permissions on revoke (ADR §6.2).
		frappe.db.delete(
			"User Permission",
			{"user": self.user, "allow": permissions.BRANCH_DOCTYPE},
		)
