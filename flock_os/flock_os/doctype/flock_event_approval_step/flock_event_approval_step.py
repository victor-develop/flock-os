# Copyright (c) 2026, Flock OS and contributors
# For license information, please see license.txt

from __future__ import annotations

from frappe.model.document import Document


class FlockEventApprovalStep(Document):
	# One row per approver in the resolved chain (FLO-7 §3.3), materialized at
	# submit time by walking `parent_group` up to the branch-root. A pure
	# lifecycle leaf: the parent `Flock Event Approval` owns the state machine +
	# scope; the step row only records one approver's decision + audit trail
	# (decided_by / decided_at / comment). Row-level reads are scoped by the
	# central `permission_query_conditions` hook on the parent doc.
	pass
