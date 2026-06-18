# Copyright (c) 2026, Flock OS and contributors
# For license information, please see license.txt

from __future__ import annotations

import frappe
from frappe.model.document import Document

from flock_os import scheduling


class FlockAnnouncement(Document):
	# Flock Announcement = a scoped broadcast content entity (FLO-8 §3). Lifecycle
	# Draft -> Scheduled -> Publishing -> Published -> Archived. Scope is the
	# branch/group/organization contract (ADR §3); audience resolution + fan-out
	# live in the scheduling service / notification fan-out (FLO-57), triggered by
	# the admin controller on publish. `linked_gathering`/`notification` are raw
	# Data refs until their target DocTypes land.

	def validate(self):
		_branch_organization_default(self)
		scheduling.validate_announcement_scope(self, scheduling.get_gateway())


def _branch_organization_default(doc: Document) -> None:
	# Cheap denormalized tenant floor (ADR §3 contract): an announcement's
	# organization mirrors its branch's organization so the whole scope shares one
	# org. The scheduling scope validator re-checks branch-in-org membership.
	if doc.organization or not doc.branch:
		return
	branch_org = frappe.db.get_value("Flock Branch", doc.branch, "organization")
	if branch_org:
		doc.organization = branch_org
