"""
Wire the ``autoincrement`` sequence as the ``name`` column DEFAULT for the
attendance backing tables (FLO-53 runtime gate).

Why this exists
---------------
The two attendance DocTypes use Frappe ``autoname: autoincrement`` (set in
[FLO-64](/FLO/issues/FLO-64) so name-less raw-SQL writes work, per the
[FLO-66](/FLO/issues/FLO-66) "preserve the raw-SQL bulk-write path" mandate).
Frappe's autoincrement naming is **sequence-based and applied in Python** during
``Document.insert()`` — it does *not* place a DB-level DEFAULT on the ``name``
column. The bulk-attendance gateway writes these tables with raw SQL that omits
``name`` (``frappe.db.bulk_insert`` + the ``INSERT … ON DUPLICATE KEY UPDATE``
upsert in :func:`FrappeBulkAttendanceGateway.increment_aggregate`), so without a
column DEFAULT the write fails::

    OperationalError: (1364, "Field 'name' doesn't have a default value")

This patch wires the already-created Frappe sequence (``<doctype>_id_seq``) as
the ``name`` column DEFAULT, so every raw insert draws the next name from the
*same* counter Frappe uses — keeping raw-SQL and Frappe-managed inserts
consistent (no split counters, no collision risk).

Idempotent: only ALTERs when the DEFAULT isn't already a ``NEXT VALUE FOR``.
Runs ``post_model_sync`` (the tables + sequences exist by then), after
``add_attendance_indexes``.
"""

from __future__ import annotations

# The two attendance DocTypes whose raw-SQL write path needs a name DEFAULT.
# (doctype_name, table_name, sequence_name). ``scrub(doctype) + "_id_seq"`` is
# Frappe's autoincrement sequence-naming convention.
TARGETS = (
	(
		"Flock Attendance Record",
		"tabFlock Attendance Record",
		"flock_attendance_record_id_seq",
	),
	(
		"Event Attendance Summary",
		"tabEvent Attendance Summary",
		"event_attendance_summary_id_seq",
	),
)


def _name_default_is_sequence(frappe, table: str) -> bool:
	"""True if the ``name`` column DEFAULT already pulls from a sequence."""
	row = frappe.db.sql(
		"""
		SELECT COLUMN_DEFAULT
		FROM information_schema.COLUMNS
		WHERE TABLE_SCHEMA = DATABASE()
		AND TABLE_NAME = %s
		AND COLUMN_NAME = 'name'
		LIMIT 1
		""",
		(table),
	)
	default = row[0][0] if row else None
	# MariaDB stores sequence defaults as "NEXT VALUE FOR `seq`".
	return bool(default and "NEXT VALUE" in str(default).upper())


def execute() -> None:
	import frappe

	for _doctype, table, sequence in TARGETS:
		if _name_default_is_sequence(frappe, table):
			continue
		frappe.db.sql(
			f"ALTER TABLE `{table}` MODIFY `name` BIGINT NOT NULL DEFAULT NEXT VALUE FOR `{sequence}`"
		)
