"""
Seed core fixtures: Flock roles + Flock Group Types (FLO-5 §4.1/§3.3).

Idempotent — safe to run repeatedly on ``bench migrate``. Creates each record
only when it does not already exist, so the seed travels to every fresh and
existing site deterministically. The seeded names are mirrored by the
``fixtures`` export config in :mod:`flock_os.hooks` so exported fixtures stay in
sync with this patch.
"""

from __future__ import annotations

import frappe

from flock_os.fixtures import FLOCK_GROUP_TYPES, FLOCK_ROLES


def execute() -> None:
	"""Create Flock roles and Flock Group Type seed values if missing."""
	for role_name in FLOCK_ROLES:
		if frappe.db.exists("Role", role_name):
			continue
		role = frappe.get_doc({"doctype": "Role", "role_name": role_name})
		role.insert(ignore_permissions=True)

	for group_type in FLOCK_GROUP_TYPES:
		if frappe.db.exists("Flock Group Type", group_type["group_type_name"]):
			continue
		doc = frappe.get_doc({"doctype": "Flock Group Type", **group_type})
		doc.insert(ignore_permissions=True)

	frappe.db.commit()
