# Copyright (c) 2026, Flock OS and contributors
# For license information, please see license.txt.

from __future__ import annotations

from frappe.model.document import Document


class FlockEngagementGameTemplate(Document):
	# Reusable, org-scoped mini-game content library (FLO-9 §13). A facilitator
	# picks a template when creating a session; the runtime clones its config
	# into the session JSON. Custom content passes through the org-level review
	# step (FLO-9 §7) before facilitators can pick it.
	pass
