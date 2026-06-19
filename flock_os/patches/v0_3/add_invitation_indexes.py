"""
Add hot-path indexes for one-time-event invitations (FLO-7 §3.6, materialized by
[FLO-79] Phase B).

Frappe DocType JSON declares the single-column ``unique`` on ``invite_token``
(the link-based RSVP dedup) + ``search_index`` flags on the scoping columns, but
the invitation eligibility lookups (``has_valid_invitation`` in
:mod:`flock_os.flock_os.doctype.flock_event_registration`) filter on composite
(gathering, invitee) / (gathering, invitee_group) predicates that need explicit
composite indexes for the 15k-scale ``Invited Only`` scope gate:

* ``tabFlock Event Invitation``
    - ``(gathering, invitee, status)`` — the direct-person eligibility lookup
      (``has_valid_invitation`` filters gathering + invitee + status in
      Sent/Accepted).
    - ``(gathering, invitee_group, status)`` — the group-subtree eligibility
      lookup (group-subtree invitations qualify a member in that subtree).

Idempotent — safe to re-run on every ``bench migrate``. The DocType table is
synced from its JSON *before* post-model-sync patches run, so it already exists
when this patch executes. Mirrors the [FLO-62] registration index patch pattern.
"""

from __future__ import annotations

# `frappe` is imported lazily inside :func:`execute` so this module stays
# import-clean under the project-level pytest gate (no Frappe site required).

# (DocType, index_name, columns_sql, unique?). The column list is the
# authoritative contract; flock_os/tests/test_registrations.py pins the
# invitation schema + scoping registration so a drift fails the project gate.
INDEXES = (
	# Direct-person eligibility lookup (has_valid_invitation direct path).
	(
		"Flock Event Invitation",
		"index_gathering_invitee_status",
		"(`gathering`, `invitee`, `status`)",
		False,
	),
	# Group-subtree eligibility lookup (has_valid_invitation subtree path).
	(
		"Flock Event Invitation",
		"index_gathering_invitee_group_status",
		"(`gathering`, `invitee_group`, `status`)",
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
