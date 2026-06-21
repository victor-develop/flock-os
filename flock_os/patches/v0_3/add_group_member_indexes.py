"""
Add hot-path composite indexes for the Flock Group Member roster (FLO-5 §3.3,
surfaced by the [FLO-454] 15k stress drill).

Frappe DocType JSON declares single-column ``search_index`` flags on the
scoping columns (``group``, ``member``, ``branch``, ``organization``), but the
notification fan-out roster resolution path filters on **composite** predicates
that the optimizer cannot serve from a single-column index. The [FLO-454] drill
proved this empirically:

    EXPLAIN SELECT member FROM `tabFlock Group Member`
    WHERE branch = 'stress-branch-1' AND status = 'Active'

    -> type: ALL, possible_keys: branch, key: NULL, rows: 15000

The single-column ``branch`` index has poor selectivity at 15k rows (~1/3 of
rows match a given branch), so the optimizer skips it and full-scans.

**What these composites actually serve.** The authoritative production read
that filters on ``(branch|group, status)`` is the **notification fan-out
audience resolver** — :func:`FrappeNotificationFanoutGateway._leaders`
(``notifications.py``), which plucks Active Leader/Co-Leader roster rows scoped
by branch or group. After [FLO-465] added the ``status = 'Active'`` predicate
to that read, the ``(branch, status)`` / ``(group, status)`` composites serve
its leftmost prefix directly (index-served ``type: ref`` instead of full scan).

The earlier [FLO-459] version of this docstring misattributed the benefit to
``is_member_in_scope`` on the registration critical path. That was incorrect:
``is_member_in_scope`` resolves scope off ``tabFlock Member.branch`` (branch
scope) or a single-column ``member`` lookup on ``tabFlock Group Member`` (group
scope) — neither path filters on ``(branch, status)`` or ``(group, status)``,
and the registration read is served by the single-column ``member`` index
(``~0.46ms / 1 row`` per the [FLO-454] evidence table). The composites here are
fan-out infrastructure, not a registration-critical-path optimization.

This patch adds the two composites the fan-out queries actually filter on:

* ``tabFlock Group Member``
    - ``(group, status)`` — the ``Own Group`` / ``Group Subtree`` fan-out
      audience path (``leaders_in_groups`` → ``_leaders`` with
      ``group IN (...)``, ``role IN (Leader, Co-Leader)``, ``status='Active'``).
    - ``(branch, status)`` — the ``Branch`` / ``Branch Subtree`` fan-out
      audience path (``leaders_in_branches`` → ``_leaders`` with
      ``branch IN (...)``, ``role IN (...)``, ``status='Active'``; the
      [FLO-454] EXPLAIN evidence above).

Idempotent — safe to re-run on every ``bench migrate``. The DocType table is
synced from its JSON *before* post-model-sync patches run, so it already
exists when this patch executes. Mirrors the [FLO-79] invitation index patch
pattern.
"""

from __future__ import annotations

# `frappe` is imported lazily inside :func:`execute` so this module stays
# import-clean under the project-level pytest gate (no Frappe site required).

# (DocType, index_name, columns_sql, unique?). The column list is the
# authoritative contract; flock_os/tests/test_registrations.py pins the
# group-member index schema so a drift fails the project gate.
INDEXES = (
	# Own Group / Group Subtree roster resolution (member_groups + roster fan-out).
	(
		"Flock Group Member",
		"idx_group_status",
		"(`group`, `status`)",
		False,
	),
	# Branch / Branch Subtree roster resolution ([FLO-454] EXPLAIN evidence).
	(
		"Flock Group Member",
		"idx_branch_status",
		"(`branch`, `status`)",
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
