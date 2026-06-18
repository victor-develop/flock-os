# Copyright (c) 2026, Flock OS and contributors
# For license information, please see license.txt

from __future__ import annotations

from frappe.model.document import Document


class EventAttendanceSummary(Document):
	# Maintained aggregate: one row per (branch, event) holding the atomically
	# incremented attendance `total` (FLO-10 §3.3/§4.2). The production
	# `FrappeBulkAttendanceGateway.increment_aggregate` writes it via raw
	# `INSERT … ON DUPLICATE KEY UPDATE` against the UNIQUE (branch, event) index
	# materialized by the v0_1 patch — never a scan-then-write. Counts are read
	# from this rollup, not via a live COUNT(*). This controller only governs
	# the Document-API / admin path + the permission surface; `total` is
	# read-only on the field to signal it is system-maintained.
	#
	# Naming: `autoincrement` (sequence-backed `bigint` PK). The
	# `ON DUPLICATE KEY UPDATE` matches on the UNIQUE (branch, event) index, not
	# on `name`, so the upsert is correct regardless of the PK value — but, as
	# on the record table, the raw insert must still supply `name` (Frappe gives
	# the `bigint primary key` no default), see FlockAttendanceRecord note.
	pass
