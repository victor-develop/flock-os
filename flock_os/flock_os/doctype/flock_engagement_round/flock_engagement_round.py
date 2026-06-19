# Copyright (c) 2026, Flock OS and contributors
# For license information, please see license.txt.

from __future__ import annotations

from frappe.model.document import Document


class FlockEngagementRound(Document):
	# One round within an engagement session (FLO-9 §3) — a quiz question, a
	# reaction-tap round, a poll option set, a bingo template row, etc. Authored
	# by the facilitator at session config time; the runtime publishes a
	# synchronized tick (FLO-9 §7 fairness) on each round's ``starts_at``.
	pass
