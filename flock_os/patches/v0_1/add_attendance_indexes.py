"""
Add composite UNIQUE indexes for the attendance backing tables (FLO-64) and
collapse the duplicate rows their absence allowed (FLO-457).

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

FLO-457 — survive a dirty DB. FLO-454's 15k drill surfaced that these indexes
were absent from the live bench, so the ``(event, attendee_ref)`` idempotency
backstop never fired and replays **double-wrote** attendance rows (and
``INSERT IGNORE`` silently duplicated summary rows). A plain ``ADD UNIQUE
INDEX`` on such a dirty table raises a duplicate-key error and aborts
``bench migrate`` — the fix could not even be applied to the DBs that needed it
most (the live stress bench's ``filter_unseen`` took 4,471 ms for a 500-item
batch — 89x the §8 p95 < 500 ms bar). So before each UNIQUE index is added,
:func:`execute` collapses any duplicate rows on that index's key:

* attendance record — keep the earliest insert (MIN name); the spurious replay
  rows are the later ones.
* attendance summary — fold each duplicate ``(branch, event)`` group into its
  survivor and **sum** their ``total`` so the maintained rollup loses no count.

The dedup is idempotent (a no-op on a clean table) and only runs when the index
is actually missing, so re-running on every ``bench migrate`` is safe. DocType
tables are synced from their JSON *before* post-model-sync patches run, so they
already exist when this patch executes (it is safe to run on a site that
already has the FLO-17 core set).
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

# FLO-457: dedup strategy to run *before* adding each UNIQUE index, keyed by
# index name. Only the **broadest** key on each table is listed — a superset
# index (e.g. the ``..., client_req_id`` one) is automatically unique once its
# prefix is deduped, so it intentionally has no entry here (the no-bench gate
# pins this). See :data:`STRATEGIES` for the implemented survivors.
DEDUP = {
	# Collapse to the earliest insert per (event, attendee_ref).
	"unique_event_attendee_ref": "keep_min_name",
	# Fold duplicate (branch, event) summary rows into the survivor, summing
	# ``total`` so the rollup never loses a count.
	"unique_branch_event": "sum_total",
}

# Implemented dedup strategies. The project-level gate pins that every value in
# :data:`DEDUP` is one of these, so an unknown strategy fails the no-bench run.
STRATEGIES = frozenset({"keep_min_name", "sum_total"})


def _columns_of(columns_sql: str) -> tuple[str, ...]:
	"""Parse "(`event`, `attendee_ref`)" -> ("event", "attendee_ref")."""
	return tuple(c.strip().strip("`") for c in columns_sql.strip("()").split(","))


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


def _collapse_keep_min_name(frappe, table: str, columns: tuple[str, ...]) -> int:
	"""Delete every non-earliest duplicate on ``columns``, keeping MIN(name).

	``name`` is the autoincrement PK (never NULL) and every column in
	``columns`` is NOT NULL (``reqd`` on the DocType — pinned by the project
	gate), so plain ``=`` join semantics hold. Idempotent: once only the min
	row per group remains the self-join matches nothing. Returns the number of
	rows deleted (0 on a clean table).
	"""
	on = " AND ".join([f"r1.`{c}` = r2.`{c}`" for c in columns] + ["r1.`name` > r2.`name`"])
	dupes = int(frappe.db.sql(f"SELECT COUNT(*) FROM `{table}` r1 INNER JOIN `{table}` r2 ON {on}")[0][0])
	if dupes:
		frappe.db.sql(f"DELETE r1 FROM `{table}` r1 INNER JOIN `{table}` r2 ON {on}")
	return dupes


def _collapse_sum_total(frappe, table: str, key_columns: tuple[str, ...]) -> int:
	"""Fold each duplicate ``key_columns`` group into its survivor (MIN name),
	summing ``total`` across the group, then delete the non-survivors.

	Two derived-table statements are used so MariaDB does not trip on "can't
	specify target table for update in FROM clause". Idempotent: once a group
	collapses to one row ``HAVING COUNT(*) > 1`` no longer matches. Returns the
	number of rows deleted (0 on a clean table).
	"""
	key = ", ".join(f"`{c}`" for c in key_columns)
	on_key = " AND ".join(f"s.`{c}` = g.`{c}`" for c in key_columns)
	# Derived table: one row per duplicate group with its survivor + summed total.
	groups = (
		f"SELECT {key}, MIN(`name`) AS keep_name, SUM(`total`) AS group_total "
		f"FROM `{table}` GROUP BY {key} HAVING COUNT(*) > 1"
	)
	dupes = int(
		frappe.db.sql(
			f"SELECT COUNT(*) FROM `{table}` s INNER JOIN ({groups}) g "
			f"ON {on_key} AND s.`name` <> g.keep_name"
		)[0][0]
	)
	if dupes:
		# group_total already includes the survivor's own total, so a straight
		# SET is correct (not total + group_total).
		frappe.db.sql(
			f"UPDATE `{table}` s INNER JOIN ({groups}) g ON s.`name` = g.keep_name "
			f"SET s.`total` = g.group_total"
		)
		frappe.db.sql(
			f"DELETE s FROM `{table}` s INNER JOIN ({groups}) g ON {on_key} AND s.`name` <> g.keep_name"
		)
	return dupes


def execute() -> None:
	import frappe

	log = frappe.logger("flock_os")
	for doctype, index_name, columns_sql in INDEXES:
		table = f"tab{doctype}"
		if _index_exists(frappe, table, index_name):
			continue
		strategy = DEDUP.get(index_name)
		collapsed: int | None
		if strategy == "keep_min_name":
			collapsed = _collapse_keep_min_name(frappe, table, _columns_of(columns_sql))
		elif strategy == "sum_total":
			collapsed = _collapse_sum_total(frappe, table, _columns_of(columns_sql))
		else:
			collapsed = None
		if collapsed:
			log.info(
				"flock_os add_attendance_indexes: collapsed %s duplicate row(s) on"
				" `%s` before adding UNIQUE index `%s` (FLO-457 dirty-DB recovery)",
				collapsed,
				table,
				index_name,
			)
		frappe.db.sql(f"ALTER TABLE `{table}` ADD UNIQUE INDEX `{index_name}` {columns_sql}")
