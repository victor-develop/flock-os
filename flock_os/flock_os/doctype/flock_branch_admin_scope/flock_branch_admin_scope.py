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
		# The allowed set = the configured branch + its descendant branches,
		# computed live from the current tree (ADR §6.2) via the canonical pure
		# subtree fn. Tree math lives in flock_os.trees / permissions only (DRY);
		# this controller just materializes the adjacency once and delegates.
		# Delegating (rather than a raw nested-set query) guarantees the *upper*
		# bound is enforced, so a sibling branch ordered after this root can
		# never leak into the admin's allowed set (ADR §6.2 tenant isolation).
		if not self.branch:
			return []
		parent_of, children_of = _materialize_branch_adjacency()
		return list(
			permissions.compute_branch_subtree(self.branch, parent_of=parent_of, children_of=children_of)
		)

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


def _materialize_branch_adjacency() -> tuple[dict[str, str | None], dict[str, list[str]]]:
	# One read of the branch tree; the pure traversal primitives in
	# flock_os.flock_os.trees consume the adjacency views (parent_of /
	# children_of). Ordered by lft so children_of lists follow tree order.
	rows = frappe.get_all("Flock Branch", fields=["name", "parent_branch"], order_by="lft")
	parent_of: dict[str, str | None] = {}
	children_of: dict[str, list[str]] = {}
	for row in rows:
		name = row["name"]
		parent = row.get("parent_branch")
		parent_of[name] = parent
		children_of.setdefault(name, [])
		if parent:
			children_of.setdefault(parent, []).append(name)
	return parent_of, children_of
