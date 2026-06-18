# Copyright (c) 2026, Flock OS and contributors
# For license information, please see license.txt

from __future__ import annotations

from frappe.model.document import Document


class FlockGroupType(Document):
	# Config/master DocType (FLO-5 §3.3). Seeded values ship via the versioned
	# patch flock_os.patches.v0_1.seed_core_fixtures (Ministry, Cell Group, ...).
	pass
