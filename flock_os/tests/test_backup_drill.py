"""Unit tests for the pure backup/restore-drill helpers ([FLO-288](/FLO/issues/FLO-288)).

Frappe-free and DB-free: these pin the table-mapping, SQL generation, TSV
parsing, parity verdict, and core-set selection logic that the live
``scripts/dev/restore-drill.sh`` relies on. The drill itself exercises the
same functions end-to-end against a real bench site.
"""

from __future__ import annotations

import pytest

from flock_os.utils.backup_drill import (
	build_count_sql,
	build_doctype_discovery_sql,
	compare_counts,
	doctype_to_table,
	parse_counts,
	select_core_doctypes,
	strip_sequence_qualifier,
)


# --------------------------------------------------------------------------- #
# doctype_to_table
# --------------------------------------------------------------------------- #
def test_doctype_to_table_prefixes_name_preserving_spaces():
	assert doctype_to_table("Flock Branch") == "tabFlock Branch"
	assert doctype_to_table("Flock Organization") == "tabFlock Organization"


def test_doctype_to_table_rejects_empty():
	with pytest.raises(ValueError):
		doctype_to_table("")


# --------------------------------------------------------------------------- #
# build_doctype_discovery_sql
# --------------------------------------------------------------------------- #
def test_discovery_sql_defaults_to_flock_wildcard_ordered():
	sql = build_doctype_discovery_sql()
	assert "SELECT name FROM `tabDocType`" in sql
	assert "name LIKE 'Flock %'" in sql
	assert sql.endswith("ORDER BY name")


def test_discovery_sql_optional_module_filter():
	sql = build_doctype_discovery_sql(module="Flock OS")
	assert "module = 'Flock OS'" in sql
	# Custom LIKE still composes with module.
	sql2 = build_doctype_discovery_sql(like="Flock Event%", module="Flock OS")
	assert "name LIKE 'Flock Event%'" in sql2
	assert "module = 'Flock OS'" in sql2


def test_discovery_sql_escapes_single_quotes_in_like():
	sql = build_doctype_discovery_sql(like="o'brien")
	assert "LIKE 'o''brien'" in sql


# --------------------------------------------------------------------------- #
# build_count_sql
# --------------------------------------------------------------------------- #
def test_count_sql_union_all_backticks_spaced_tables():
	sql = build_count_sql(["Flock Branch", "Flock Group"])
	assert "SELECT 'Flock Branch' AS dt, COUNT(*) AS n FROM `tabFlock Branch`" in sql
	assert "SELECT 'Flock Group' AS dt, COUNT(*) AS n FROM `tabFlock Group`" in sql
	assert " UNION ALL " in sql
	# Exactly one UNION ALL join between two parts.
	assert sql.count("UNION ALL") == 1


def test_count_sql_single_doctype_has_no_union():
	sql = build_count_sql(["Flock Member"])
	assert "UNION ALL" not in sql
	assert "`tabFlock Member`" in sql


def test_count_sql_empty_input_is_empty_string():
	assert build_count_sql([]) == ""


def test_count_sql_quotes_doctype_name_literals_safely():
	# A doctype containing a quote must be escaped so it can't break the literal.
	sql = build_count_sql(["X Y'Z"])
	assert "'X Y''Z'" in sql


# --------------------------------------------------------------------------- #
# parse_counts
# --------------------------------------------------------------------------- #
def test_parse_counts_tsv_to_dict():
	raw = "Flock Branch\t1\nFlock Group\t1\nFlock Member\t0\n"
	assert parse_counts(raw) == {"Flock Branch": 1, "Flock Group": 1, "Flock Member": 0}


def test_parse_counts_ignores_blank_and_whitespace_lines():
	raw = "\nFlock Branch\t3\n\n   \nFlock Group\t2\n"
	assert parse_counts(raw) == {"Flock Branch": 3, "Flock Group": 2}


def test_parse_counts_strips_crlf_and_padding():
	raw = "Flock Branch\t 5 \r\n"
	assert parse_counts(raw) == {"Flock Branch": 5}


def test_parse_counts_rejects_wrong_arity():
	with pytest.raises(ValueError):
		parse_counts("Flock Branch\t1\textra\n")


def test_parse_counts_rejects_non_integer():
	with pytest.raises(ValueError):
		parse_counts("Flock Branch\tnan\n")


# --------------------------------------------------------------------------- #
# compare_counts
# --------------------------------------------------------------------------- #
def test_compare_counts_equal_dicts_parity_ok():
	src = {"Flock Branch": 1, "Flock Group": 1}
	ok, mismatches = compare_counts(src, dict(src))
	assert ok is True
	assert mismatches == []


def test_compare_counts_detects_value_divergence_with_delta():
	src = {"Flock Branch": 2}
	rst = {"Flock Branch": 1}
	ok, mismatches = compare_counts(src, rst)
	assert ok is False
	assert len(mismatches) == 1
	assert "Flock Branch: source=2 restored=1" in mismatches[0]
	assert "(delta=-1)" in mismatches[0]


def test_compare_counts_missing_in_restored_counts_as_zero():
	src = {"Flock Branch": 1}
	rst = {}
	ok, mismatches = compare_counts(src, rst)
	assert ok is False
	assert any("Flock Branch: source=1 restored=0" in m for m in mismatches)


def test_compare_counts_extra_in_restored_also_fails():
	src = {}
	rst = {"Flock Group": 1}
	ok, mismatches = compare_counts(src, rst)
	assert ok is False
	assert any("Flock Group: source=0 restored=1" in m for m in mismatches)


def test_compare_counts_mismatches_sorted_for_stable_output():
	src = {"Zeta": 1, "Alpha": 1, "Mid": 1}
	rst = {"Zeta": 0, "Alpha": 0, "Mid": 0}
	_, mismatches = compare_counts(src, rst)
	names = [m.split(":")[0] for m in mismatches]
	assert names == ["Alpha", "Mid", "Zeta"]


# --------------------------------------------------------------------------- #
# select_core_doctypes
# --------------------------------------------------------------------------- #
def test_select_core_defaults_to_all_present_sorted():
	present = ["Flock Group", "Flock Branch", "Flock Member"]
	assert select_core_doctypes(present) == ["Flock Branch", "Flock Group", "Flock Member"]


def test_select_core_intersects_requested_with_present():
	present = ["Flock Branch", "Flock Group", "Flock Member"]
	requested = ["Flock Branch", "Flock Member", "Flock Event Registration", "Member"]
	# Only the ones that actually exist survive; "Member"/"Event Registration"
	# are absent in this release and dropped, not fatal.
	assert select_core_doctypes(present, requested) == ["Flock Branch", "Flock Member"]


def test_select_core_dedupes_requested():
	present = ["Flock Branch", "Flock Group"]
	requested = ["Flock Branch", "Flock Branch", "Flock Group"]
	assert select_core_doctypes(present, requested) == ["Flock Branch", "Flock Group"]


def test_select_core_with_empty_requested_returns_empty():
	# Explicit empty requested set -> nothing to check (caller may decide to
	# fall back to full parity).
	assert select_core_doctypes(["Flock Branch"], []) == []


# --------------------------------------------------------------------------- #
# strip_sequence_qualifier (dump cross-DB portability — mirrored by backup.sh)
# --------------------------------------------------------------------------- #
def test_strip_sequence_qualifier_removes_source_db_prefix():
	raw = (
		"  `name` bigint(20) NOT NULL DEFAULT nextval(`_aa24ee6d19b92700`.`event_attendance_summary_id_seq`),"
	)
	out = strip_sequence_qualifier(raw)
	assert out == ("  `name` bigint(20) NOT NULL DEFAULT nextval(`event_attendance_summary_id_seq`),")


def test_strip_sequence_qualifier_handles_multiple_sequences_in_text():
	text = "nextval(`sourcedb`.`a_seq`)\nnextval(`sourcedb`.`another_seq`)\n"
	assert strip_sequence_qualifier(text) == "nextval(`a_seq`)\nnextval(`another_seq`)\n"


def test_strip_sequence_qualifier_idempotent_on_already_portable():
	line = "nextval(`event_attendance_summary_id_seq`)"
	# Already unqualified -> unchanged.
	assert strip_sequence_qualifier(line) == line


def test_strip_sequence_qualifier_leaves_unrelated_lines_alone():
	text = "INSERT INTO `tabFlock Branch` VALUES ('B1');\n-- a comment\n"
	assert strip_sequence_qualifier(text) == text


def test_strip_sequence_qualifier_does_not_touch_non_nextval_qualified_refs():
	# A different function call with a db-qualified arg is NOT touched.
	text = "SELECT foo(`db`.`tbl`)"
	assert strip_sequence_qualifier(text) == text
