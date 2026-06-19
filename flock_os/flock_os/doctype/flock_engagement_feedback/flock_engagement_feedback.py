# Copyright (c) 2026, Flock OS and contributors
# For license information, please see license.txt.

from __future__ import annotations

from frappe.model.document import Document


class FlockEngagementFeedback(Document):
	# Normalized questionnaire feedback child of Flock Attendance Record (FLO-9
	# §5 / §13). One row per (attendance_record, round, feedback_kind). Poll
	# choices, word-cloud terms, Q&A questions/upvotes, and slider values all
	# share this shape so the aggregation queries stay uniform. Free text
	# (word-cloud / Q&A) is retained until the gathering is archived (§14.3) and
	# passes through moderation before public display (§7).
	pass
