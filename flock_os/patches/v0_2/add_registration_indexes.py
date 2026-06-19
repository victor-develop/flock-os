"""
Add composite UNIQUE indexes for one-time-event registration (FLO-7 §3.5 / §5,
materialized by [FLO-62]).

Frappe DocType JSON can declare single-column ``search_index`` / ``unique``
constraints via field flags, but **composite** UNIQUE indexes need explicit
DDL. The scoped-registration service (FLO-62,
:mod:`flock_os.flock_os.doctype.flock_event_registration`) consumes these exact
indexes — they are part of the locked scale contract (FLO-7 §3.5):

* ``tabFlock Event Registration``
    - ``UNIQUE (gathering, registrant)`` — idempotency backstop: one
      registration per person per event so an at-least-once replay (queue
      retry, duplicate click) never double-counts (§5 #4).
    - ``(gathering, registration_status)`` — hot-path capacity + waitlist reads
      (the dashboard reads the counter, but a scoped list still filters by
      status at 15k).
    - ``(branch, registered_at)`` — branch-scoped registration roll reads.

Idempotent — safe to re-run on every ``bench migrate``. DocType tables are
synced from their JSON *before* post-model-sync patches run, so they already
exist when this patch executes.
"""

from __future__ import annotations

# `frappe` is imported lazily inside :func:`execute` so this module stays
# import-clean under the project-level pytest gate (no Frappe site required),
# letting ``flock_os/tests/test_registrations.py`` pin the :data:`INDEXES`
# contract without a running bench.

# (DocType, index_name, columns_sql, unique?). The column list is the
# authoritative contract; flock_os/tests/test_registrations.py pins these
# tuples so a drift between the patch and the registration expectations fails
# the project-level gate.
INDEXES = (
	# §5 #4 idempotency — one registration per (gathering, registrant).
	(
		"Flock Event Registration",
		"unique_gathering_registrant",
		"(`gathering`, `registrant`)",
		True,
	),
	# Hot-path capacity/waitlist/status reads (non-unique).
	(
		"Flock Event Registration",
		"index_gathering_status",
		"(`gathering`, `registration_status`)",
		False,
	),
	# Branch-scoped registration roll reads (non-unique).
	(
		"Flock Event Registration",
		"index_branch_registered_at",
		"(`branch`, `registered_at`)",
		False,
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

	for doctype, index_name, columns, unique in INDEXES:
		table = f"tab{doctype}"
		if _index_exists(frappe, table, index_name):
			continue
		kind = "UNIQUE INDEX" if unique else "INDEX"
		frappe.db.sql(f"ALTER TABLE `{table}` ADD {kind} `{index_name}` {columns}")
