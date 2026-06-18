# Copyright (c) 2026, Flock OS and contributors
# For license information, please see license.txt

from __future__ import annotations

from frappe.model.document import Document


class FlockAnnouncementChannel(Document):
	# Flock Announcement Channel = the per-channel discriminator row of an
	# announcement's `channels` child table (FLO-8 §3 / §4.4). One row per
	# delivery channel (In-App / Push / Email / SMS). A pure config leaf: the
	# announcement owns lifecycle + scope; the channel row only records intent.
	pass
