# Copyright (c) 2026, Flock OS and contributors
# For license information, please see license.txt

from __future__ import annotations

import frappe
from frappe.model.document import Document

from flock_os import gatherings

# Safety cap on the parent_group walk when denormalizing ``group_path``. A real
# group tree is shallow; the guard only defends against a malformed/cyclic
# adjacency slipping past Frappe's nested-set invariants.
_GROUP_PATH_MAX_DEPTH = 64


class FlockGathering(Document):
	# Flock Gathering = the canonical event/gathering entity (FLO-6 §3.2,
	# ADR §3). The single source of truth for "a meeting happened / will
	# happen" — routine gatherings (FLO-6) and one-time events (FLO-7) are both
	# rows here. Submittable (§4): Frappe docstatus 0/1/2 overlays open/locked/
	# voided; the domain ``status`` field overlays the reporting lifecycle
	# (Scheduled -> Held -> Reported -> Confirmed, Cancelled terminal).
	#
	# This controller enforces the entity invariants (scoping contract + state
	# machine). The REST actions that *drive* transitions + emit the report
	# events (mark held / submit report / confirm / reject / cancel) are the
	# leader reporting workflow ([FLO-56](/FLO/issues/FLO-56)). Row-level reads
	# are scoped by the central ``permission_query_conditions`` hook (this
	# DocType is registered in ``flock_os.permissions.SCOPED_DOCTYPES``).

	def validate(self):
		self._denormalize_tenant_floor()
		self._validate_branch_group_binding()
		self._validate_status_transition()
		self._denormalize_group_path()

	# ------------------------------------------------------------------ #
	# Scoping contract (ADR §3 / §4.2)
	# ------------------------------------------------------------------ #
	def _denormalize_tenant_floor(self):
		# Cheap denormalized tenant floor (ADR §3): a gathering's organization
		# mirrors its branch's organization so the whole group subtree shares
		# one org. Same pattern as Flock Group.
		if self.organization:
			return
		if self.branch:
			branch_org = frappe.db.get_value("Flock Branch", self.branch, "organization")
			if branch_org:
				self.organization = branch_org

	def _validate_branch_group_binding(self):
		# ADR §4.2: a gathering is branch-bound to its group — its ``branch``
		# must equal its group's ``branch`` (the group subtree is branch-bound).
		group_branch = frappe.db.get_value("Flock Group", self.group, "branch") if self.group else None
		try:
			gatherings.validate_gathering_branch_binding(
				group_branch=group_branch, gathering_branch=self.branch
			)
		except gatherings.FlockGatheringError as exc:
			frappe.throw(str(exc), frappe.ValidationError)

	# ------------------------------------------------------------------ #
	# Reporting state machine (FLO-6 §4)
	# ------------------------------------------------------------------ #
	def _validate_status_transition(self):
		before = self.get_doc_before_save()
		from_status = before.status if before is not None else None
		try:
			gatherings.validate_status_transition(from_status=from_status, to_status=self.status)
		except gatherings.FlockGatheringError as exc:
			frappe.throw(str(exc), frappe.ValidationError)

	# ------------------------------------------------------------------ #
	# group_path denormalization (ADR §9 — roll-up helper only, no perms)
	# ------------------------------------------------------------------ #
	def _denormalize_group_path(self):
		# Walk parent_group from the gathering's group up to its root, then emit
		# the root-first slash-delimited path (gatherings.build_group_path).
		chain = self._group_path_to_root()
		self.group_path = gatherings.build_group_path(chain)

	def _group_path_to_root(self) -> list[str]:
		if not self.group:
			return []
		chain: list[str] = [self.group]
		seen: set[str] = {self.group}
		current = self.group
		for _ in range(_GROUP_PATH_MAX_DEPTH):
			parent = frappe.db.get_value("Flock Group", current, "parent_group")
			if not parent:
				break
			if parent in seen:  # defensive — cycles cannot form in a nested set
				break
			chain.append(parent)
			seen.add(parent)
			current = parent
		return chain
