"""Pure helpers for the backup/restore parity drill ([FLO-288](/FLO/issues/FLO-288)).

The restore-drill proves a backup is restorable by asserting row-count parity
between the source site and the restored site across the core ``Flock *``
DocTypes. The live drill (``scripts/dev/restore-drill.sh``) does the heavy
lifting in bash + raw SQL so it is **app-version and VM independent** — it never
imports the app or frappe, so it runs identically against the local bench and
prod regardless of which DocTypes a given release ships.

This module holds the genuinely non-trivial *pure* logic that the drill and its
unit tests share, kept frappe-free (stdlib only) so it runs under plain pytest
in the project QA gate without a bench site:

- :func:`doctype_to_table` — Frappe table-name derivation (``tab`` + name).
- :func:`build_doctype_discovery_sql` / :func:`build_count_sql` — generate the
  raw-SQL statements the drill pipes through the mysql client.
- :func:`parse_counts` — turn ``mysql -N -B`` TSV into a ``{doctype: count}``.
- :func:`compare_counts` — the parity verdict + human-readable diff.
- :func:`select_core_doctypes` — intersect a requested "core" subset against the
  DocTypes actually present in a site (honours the ticket's named core set while
  staying forward-compatible with DocTypes added later).

The coverage ratchet intentionally omits ``flock_os/utils/*`` (this is a
bench-only helper surface, like :mod:`flock_os.utils.smoke_fixtures`); the logic
is instead pinned by :mod:`flock_os.tests.test_backup_drill`.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

TAB = "tab"
TSV_SEP = "\t"

# Matches a SEQUENCE nextval() DEFAULT that mysqldump qualifies with the source
# DB name, e.g. ``nextval(`_sourcedb`.`event_attendance_summary_id_seq`)``.
# ``backup.sh`` mirrors this exact regex (see _normalize) so the dump restores
# into a differently-named database; a unit test pins the contract.
NEXTVAL_QUALIFIED_RE = re.compile(r"nextval\(`[^`]+`\.`([^`]+)`\)")


def strip_sequence_qualifier(sql_text: str) -> str:
	"""Drop the source-DB qualifier from SEQUENCE nextval() defaults.

	mysqldump writes ``nextval(`_sourcedb`.`seq`)`` into ``DEFAULT`` clauses.
	Loaded into a database with a different name, that triggers a privilege
	error (the target user cannot read the source DB's sequence) — so a backup
	is only restorable into the original DB name, a DR landmine. Stripping the
	qualifier resolves the sequence against whichever DB loads the dump (the
	dump creates sequences unqualified, earlier in the file). Pure + idempotent.
	"""
	return NEXTVAL_QUALIFIED_RE.sub(r"nextval(`\1`)", sql_text)


def _sql_str(value: str) -> str:
	"""Render a SQL string literal with single-quote escaping (MySQL style)."""
	return "'" + value.replace("'", "''") + "'"


def doctype_to_table(doctype: str) -> str:
	"""Return the MariaDB table name for a Frappe DocType.

	Frappe prefixes the DocType name with ``tab`` verbatim — spaces and all — so
	``"Flock Branch"`` -> ``"tabFlock Branch"``. Tables with spaces MUST be
	backtick-quoted in SQL (see :func:`build_count_sql`).
	"""
	if not doctype:
		raise ValueError("doctype must be non-empty")
	return f"{TAB}{doctype}"


def build_doctype_discovery_sql(*, like: str = "Flock %", module: str | None = None) -> str:
	"""SQL that lists the DocType names to include in the parity check.

	Defaults to every ``Flock %`` DocType present in the source site — this is
	version independent: whatever the backed-up release ships, the restored site
	must match it. Pass ``module`` to further restrict (e.g. ``"Flock OS"``).
	"""
	clauses: list[str] = [f"name LIKE {_sql_str(like)}"]
	if module is not None:
		clauses.append(f"module = {_sql_str(module)}")
	return "SELECT name FROM `tabDocType` WHERE " + " AND ".join(clauses) + " ORDER BY name"


def build_count_sql(doctypes: Iterable[str]) -> str:
	"""Build a single ``SELECT ... UNION ALL`` counting rows for each DocType.

	Empty input yields an empty string (the drill treats that as "nothing to
	compare" rather than emitting invalid SQL). Table names are backtick-quoted
	so the space in e.g. ``tabFlock Branch`` is safe.
	"""
	docs = list(doctypes)
	if not docs:
		return ""
	parts = [f"SELECT {_sql_str(d)} AS dt, COUNT(*) AS n FROM `{doctype_to_table(d)}`" for d in docs]
	return " UNION ALL ".join(parts)


def parse_counts(raw: str) -> dict[str, int]:
	"""Parse ``mysql --skip-column-names --batch`` TSV into ``{doctype: count}``.

	Tolerates trailing whitespace/blank lines. Raises ``ValueError`` on a row
	that does not split into exactly two columns or whose count is non-integer,
	so a malformed restore is caught loudly rather than silently dropped.
	"""
	counts: dict[str, int] = {}
	for line in raw.splitlines():
		row = line.rstrip("\r")
		if not row.strip():
			continue
		cols = row.split(TSV_SEP)
		if len(cols) != 2:
			raise ValueError(f"expected 2 columns, got {len(cols)}: {row!r}")
		counts[cols[0].strip()] = int(cols[1].strip())
	return counts


def compare_counts(source: dict[str, int], restored: dict[str, int]) -> tuple[bool, list[str]]:
	"""Compare two ``{doctype: count}`` maps; return ``(parity_ok, mismatches)``.

	Parity holds iff every DocType present in *either* site has the same count in
	both (missing-from-one implies a count of 0 there). ``mismatches`` is a
	human-readable, sorted list describing each divergence — empty when parity
	holds. A restored site with *extra* DocTypes not in the source also fails:
	a faithful restore adds nothing.
	"""
	mismatches: list[str] = []
	for key in sorted(set(source) | set(restored)):
		s = source.get(key, 0)
		r = restored.get(key, 0)
		if s != r:
			mismatches.append(f"{key}: source={s} restored={r} (delta={r - s:+d})")
	return (len(mismatches) == 0, mismatches)


def select_core_doctypes(present: Iterable[str], requested: Iterable[str] | None = None) -> list[str]:
	"""Return the DocTypes to parity-check, in stable (sorted) order.

	With ``requested=None`` every present DocType is checked (full parity — the
	default, most thorough). Otherwise the result is the intersection of the
	requested "core" set and what actually exists in the site, so the ticket's
	named core list (e.g. ``Flock Branch``) is honoured without erroring when a
	DocType is absent in a given release (e.g. ``Flock Event Registration`` pre-
	merge). Unknown requested names are dropped, not fatal.
	"""
	present_set = set(present)
	if requested is None:
		return sorted(present_set)
	return sorted({d for d in requested if d in present_set})
