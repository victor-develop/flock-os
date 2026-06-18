"""
Add composite UNIQUE indexes for the attendance backing tables (FLO-64).

Frappe DocType JSON can declare single-column ``search_index`` / ``unique``
constraints via field flags, but **composite** UNIQUE indexes need explicit DDL.
The bulk-attendance service (FLO-15, :mod:`flock_os.reporting`) consumes these
exact indexes — they are part of the locked scale contract (FLO-10 §4.1):

* ``tabFlock Attendance Record``
    - ``UNIQUE (event, attendee_ref)``                — dedupe backstop
    - ``UNIQUE (event, attendee_ref, client_req_id)`` — idempotency index
      consumed by ``FrappeBulkAttendanceGateway.filter_unseen``
* ``tabEvent Attendance Summary``
    - ``UNIQUE (branch, event)`` — drives the
      ``INSERT ... ON DUPLICATE KEY UPDATE total = total + VALUES(total)``
      upsert in ``increment_aggregate`` (correct under concurrent batches).

Idempotent — safe to re-run on every ``bench migrate``. DocType tables are
synced from their JSON *before* post-model-sync patches run, so they already
exist when this patch executes (it is safe to run on a site that already has the
FLO-17 core set).
"""

from __future__ import annotations

# `frappe` is imported lazily inside :func:`execute` so this module stays
# import-clean under the project-level pytest gate (no Frappe site required),
# letting ``flock_os/tests/test_doctype_schema.py`` pin the :data:`INDEXES`
# contract without a running bench.

# (DocType, index_name, columns_sql). The column list is the authoritative
# contract; flock_os/tests/test_doctype_schema.py pins these tuples so a drift
# between the patch and the gateway's expectations fails the project-level gate.
INDEXES = (
	(
		"Flock Attendance Record",
		"unique_event_attendee_ref",
		"(`event`, `attendee_ref`)",
	),
	(
		"Flock Attendance Record",
		"unique_event_attendee_req",
		"(`event`, `attendee_ref`, `client_req_id`)",
	),
	(
		"Event Attendance Summary",
		"unique_branch_event",
		"(`branch`, `event`)",
	),
)


def _index_exists(frappe, table: str, index_name: str) -> bool:
	return bool(
		frappe.db.sql(
			"""
			SELECT 1
			FROM information_schema.STATISTICS
			WHERE table_schema = DATABASE()
			AND table_name = %s
			AND index_name = %s
			LIMIT 1
			""",
			(table, index_name),
		)
	)


def execute() -> None:
	import frappe

	for doctype, index_name, columns in INDEXES:
		table = f"tab{doctype}"
		if _index_exists(frappe, table, index_name):
			continue
		frappe.db.sql(f"ALTER TABLE `{table}` ADD UNIQUE INDEX `{index_name}` {columns}")
