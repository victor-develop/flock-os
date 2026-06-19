# Copyright (c) 2026, Flock OS and contributors
# For license information, please see license.txt.

from __future__ import annotations

from frappe.model.document import Document


class FlockEngagementSession(Document):
	# Flock Engagement Session = one live engagement instance bound to a Flock
	# Gathering (FLO-9 §2). Lifecycle draft → scheduled → open → closing →
	# closed → archived; attendance is materialized on the open → closed
	# transition by the engagement runtime (FLO-11). This controller enforces
	# the entity invariants (scoping contract + kind/engagement_type binding +
	# window sanity); the REST actions that drive transitions are the player /
	# facilitator workflow in ``flock_os.engagement_api``. Row-level reads are
	# scoped by the central ``permission_query_conditions`` hook (registered in
	# ``flock_os.permissions.SCOPED_DOCTYPES``).
	pass
