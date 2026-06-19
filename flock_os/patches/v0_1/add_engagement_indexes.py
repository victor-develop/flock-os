"""
Add composite UNIQUE / reporting indexes for the engagement runtime (FLO-11).

Frappe DocType JSON can declare single-column ``search_index`` / ``unique``
constraints via field flags, but **composite** UNIQUE indexes need explicit DDL.
The engagement runtime (FLO-9 design §5 / §13, :mod:`flock_os.engagement`) and
the cross-source dedup contract (ADR-0001 §9) consume these exact indexes —
they are part of the locked scale contract:

* ``tabFlock Attendance Record``
    - ``UNIQUE (engagement_session, attendee_key)`` — the engagement idempotency
      backstop within one session (FLO-9 §5). One attendance credit per
      (session, attendee) pair — a player cannot be counted twice in the same
      session even across retries / replays.
    - ``UNIQUE (branch, gathering, member)`` — the ADR §9 cross-source dedup
      composite. A manual roster credit and an engagement credit for the same
      ``(branch, gathering, member)`` collapse to one attendance row via upsert.
    - ``INDEX (branch, organization, gathering, submitted_at)`` — the branch-
      leading reporting index (ADR §8 / FLO-9 §5).

Idempotent — safe to re-run on every ``bench migrate``. DocType tables are
synced from their JSON *before* post-model-sync patches run, so they already
exist when this patch executes. ``member`` is nullable for visitor rows; the
unique index allows multiple NULLs (ANSI SQL), so visitor dedup rides the
``(engagement_session, attendee_key)`` index instead.
"""

from __future__ import annotations

# `frappe` is imported lazily inside :func:`execute` so this module stays
# import-clean under the project-level pytest gate (no Frappe site required).

# (DocType, index_name, columns_sql, unique?). The column list is the
# authoritative contract; flock_os/tests/test_doctype_schema.py pins these
# tuples so a drift between the patch and the runtime expectations fails the
# project-level gate.
INDEXES = (
	(
		"Flock Attendance Record",
		"unique_engagement_session_attendee_key",
		"(`engagement_session`, `attendee_key`)",
		True,
	),
	(
		"Flock Attendance Record",
		"unique_branch_gathering_member",
		"(`branch`, `gathering`, `member`)",
		True,
	),
	(
		"Flock Attendance Record",
		"idx_branch_org_gathering_submitted",
		"(`branch`, `organization`, `gathering`, `submitted_at`)",
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
		unique_sql = "UNIQUE" if unique else ""
		frappe.db.sql(
			f"ALTER TABLE `{table}` ADD {unique_sql} INDEX `{index_name}` {columns}".replace("  ", " ")
		)
