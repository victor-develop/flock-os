# Copyright (c) 2026, Flock OS and contributors
# For license information, please see license.txt

from __future__ import annotations

from frappe.model.document import Document


class FlockAttendanceRecord(Document):
	# One attendance row per (event, attendee_ref). Backs the queue-based bulk
	# write path (FLO-10 §3): the production `FrappeBulkAttendanceGateway` writes
	# this table via raw `frappe.db.bulk_insert` (bypassing per-doc validate/save
	# for the 200 writes/sec §8 bar), so this controller only governs the
	# Document-API / admin-correction path + the permission surface. Row-level
	# scoping rides native Frappe User Permissions on the `branch` Link (ADR
	# §6.2); the two UNIQUE compound indexes (event, attendee_ref) and
	# (event, attendee_ref, client_req_id) are materialized by the v0_1 patch.
	#
	# Naming: `autoincrement` (sequence-backed `bigint` PK — correct Frappe
	# token, exactly what `MariaDBTable.create` checks). NOTE for the live write
	# path (FLO-53): Frappe makes the `name` column `bigint primary key` WITHOUT
	# a default and WITHOUT AUTO_INCREMENT (the sequence is consumed only in the
	# app layer by `set_new_name`, which raw `bulk_insert` bypasses). So the
	# gateway's raw insert MUST supply `name` (e.g. `frappe.db.get_next_sequence_val`)
	# — omitting it fails regardless of the naming rule.
	pass
