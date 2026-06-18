"""
Seed Flock Gathering Type fixtures (FLO-6 §3.1, [FLO-54](/FLO/issues/FLO-54)).

Idempotent — safe to run repeatedly on ``bench migrate``. Creates each gathering
type only when it does not already exist, so the seed travels to every fresh and
existing site deterministically. The seeded names are mirrored by the
``fixtures`` export config in :mod:`flock_os.hooks` so exported fixtures stay in
sync with this patch. Runs ``post_model_sync`` (see :mod:`flock_os.patches`)
because it writes the ``Flock Gathering Type`` table that the model sync creates.
"""

from __future__ import annotations

import frappe

from flock_os.fixtures import FLOCK_GATHERING_TYPES


def execute() -> None:
	"""Create Flock Gathering Type seed values if missing."""
	for gathering_type in FLOCK_GATHERING_TYPES:
		if frappe.db.exists("Flock Gathering Type", gathering_type["gathering_type_name"]):
			continue
		doc = frappe.get_doc({"doctype": "Flock Gathering Type", **gathering_type})
		doc.insert(ignore_permissions=True)

	frappe.db.commit()
