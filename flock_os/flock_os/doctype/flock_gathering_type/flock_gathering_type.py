# Copyright (c) 2026, Flock OS and contributors
# For license information, please see license.txt

from __future__ import annotations

from frappe.model.document import Document


class FlockGatheringType(Document):
	# Config/master DocType (FLO-6 §3.1). Defines what a gathering is (e.g.
	# "Sunday Service", "Cell Group", "Youth"). Reusable org-wide (branch null)
	# or branch-overridable. Seeded values ship via the versioned patch
	# flock_os.patches.v0_1.seed_gathering_fixtures. Read by all leaders;
	# managed by branch/org admins.
	pass
