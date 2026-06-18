# Copyright (c) 2026, Flock OS and contributors
# For license information, please see license.txt

from __future__ import annotations

from frappe.model.document import Document


class FlockAuditLog(Document):
	# Compliance trail for permission-sensitive ops (ADR §4.7 #2): role/branch-
	# scope changes, approvals, scope overrides (system_query), UP subtree sync.
	# `branch` is nullable so org-wide events (an Org Admin action) are captured.
	# Written by flock_os.permissions.system_query + the branch-axis syncer; read
	# org-wide by Flock Auditor (FLO-5 §4.1).
	pass
