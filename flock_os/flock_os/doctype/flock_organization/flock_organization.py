# Copyright (c) 2026, Flock OS and contributors
# For license information, please see license.txt

from __future__ import annotations

import frappe
from frappe.model.document import Document


class FlockOrganization(Document):
	# Frappe page path / tree-free singleton identity (FLO-5 §3.1, ADR §3/§6.1).
	pass

	def validate(self):
		# 1 site = 1 customer org (CEO-ratified, FLO-5 §8.1). `autoname: "FIXED"`
		# (ADR §3) enforces the singleton at the DB level via primary-key uniqueness
		# on `name`. This belt-and-suspenders check surfaces a friendly error before
		# the DB raise and resolves the concurrent-setup race the old field-naming
		# path had.
		existing = frappe.db.count(
			"Flock Organization",
			filters={"name": ["!=", self.name or ""]},
		)
		if existing:
			frappe.throw(
				"Only one Flock Organization is allowed per site (1 site = 1 customer org; FLO-5 §8.1).",
				frappe.ValidationError,
			)
