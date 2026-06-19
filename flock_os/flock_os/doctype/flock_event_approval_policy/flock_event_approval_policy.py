# Copyright (c) 2026, Flock OS and contributors
# For license information, please see license.txt

from __future__ import annotations

from frappe.model.document import Document


class FlockEventApprovalPolicy(Document):
	# Configurable approval-chain rules (FLO-7 §3.4). Scoped per branch/org; the
	# nearest policy to the gathering's branch wins. A pure config master: the
	# `Flock Event Approval` controller reads a policy row and builds the frozen
	# `ApprovalPolicy` struct the pure chain resolver consumes. Leader read /
	# Branch-Admin+ write (FLO-7 §6.1).
	pass
