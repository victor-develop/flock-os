"""
Project-level schema-contract tests for the Flock OS core DocTypes (FLO-17).

These run under plain ``pytest`` (no Frappe site / bench required). They parse
each DocType JSON and assert it conforms to the **locked canonical model**
(ADR-0001 rev 3 + FLO-5 §3/§5): exact DocType names, the scoping contract
(every visible-per-branch DocType carries a ``branch`` Link named ``branch``),
tree configuration, link targets, and the field-level permission markers. This
is the project-level enforcement of "Schema matches ADR FLO-4 exactly".

Frappe-level integration tests (CRUD, inserts, DB uniqueness) live alongside
each DocType and run via ``bench run-tests``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

DOCTYPE_DIR = Path(__file__).resolve().parent.parent / "flock_os" / "doctype"

CORE_DOCTYPES = (
	"Flock Organization",
	"Flock Branch",
	"Flock Group Type",
	"Flock Group",
	"Flock Member",
	"Flock Group Member",
)

# Permission/audit infra owned by FLO-20 (ADR §6 / FLO-5 §5). Separate from the
# core data-model catalog so the tree/scoping contract parametrization above is
# unaffected; these get dedicated contract tests below.
PERMISSION_DOCTYPES = (
	"Flock Audit Log",
	"Flock Branch Admin Scope",
)


def _slug(name: str) -> str:
	return name.lower().replace(" ", "_")


def _load(name: str) -> dict:
	path = DOCTYPE_DIR / _slug(name) / f"{_slug(name)}.json"
	assert path.exists(), f"Missing DocType JSON for {name}: {path}"
	with path.open() as f:
		return json.load(f)


@pytest.fixture(scope="module")
def schemas() -> dict[str, dict]:
	return {name: _load(name) for name in CORE_DOCTYPES}


# --------------------------------------------------------------------------- #
# Catalog: exactly the 6 core DocTypes, Flock-prefixed, in the flock_os module
# --------------------------------------------------------------------------- #


def test_core_doctype_catalog_is_complete(schemas):
	# FLO-5 §5 build list (org/group/member subset). Flock Audit Log + Flock
	# Branch Admin Scope are permission/audit infra owned by FLO-20.
	assert set(schemas) == set(CORE_DOCTYPES)


@pytest.mark.parametrize("name", CORE_DOCTYPES)
def test_doctypes_use_flock_prefix_and_module(schemas, name):
	doc = schemas[name]
	assert doc["doctype"] == "DocType"
	assert doc["name"] == name
	assert name.startswith("Flock ")
	assert doc["module"] == "flock_os"


# --------------------------------------------------------------------------- #
# Scoping contract (FLO-5 §3.5) — every visible-per-branch DocType carries a
# `branch` Link named exactly "branch"; Flock Member labels it "Home Branch".
# --------------------------------------------------------------------------- #


def _field(doc: dict, fieldname: str) -> dict:
	matches = [f for f in doc["fields"] if f["fieldname"] == fieldname]
	assert matches, f"field {fieldname!r} missing"
	return matches[0]


SCOPED_DOCTYPES = ("Flock Group", "Flock Member", "Flock Group Member")


@pytest.mark.parametrize("name", SCOPED_DOCTYPES)
def test_scoping_contract_branch_link(schemas, name):
	# FLO-5 §3.5 QA gate: downstream visible-per-branch DocTypes carry a `branch`
	# Link named exactly "branch". (Flock Branch itself IS the key — no self-link.)
	branch = _field(schemas[name], "branch")
	assert branch["fieldtype"] == "Link"
	assert branch["options"] == "Flock Branch"
	assert branch.get("reqd") == 1
	assert branch.get("search_index") == 1


def test_flock_branch_is_the_scoping_key(schemas):
	# Flock Branch carries the tenant floor (`organization`) + tree adjacency; it
	# does not need (and per the contract must not duplicate) a `branch` self-link.
	doc = schemas["Flock Branch"]
	assert _field(doc, "organization")["options"] == "Flock Organization"
	assert not [f for f in doc["fields"] if f["fieldname"] == "branch"]


def test_member_branch_field_is_labeled_home_branch(schemas):
	# FLO-5 §3.5 Rev 3: fieldname stays `branch`, label is "Home Branch".
	branch = _field(schemas["Flock Member"], "branch")
	assert branch["label"] == "Home Branch"


def test_group_level_doctypes_carry_group_link(schemas):
	# ADR §3 contract: group-level docs carry `group` -> Flock Group. Flock Group
	# scopes on its own nested set (no `group` self-link); the membership edge does.
	group = _field(schemas["Flock Group Member"], "group")
	assert group["fieldtype"] == "Link"
	assert group["options"] == "Flock Group"


def test_group_member_branch_is_denormalized_readonly(schemas):
	# Architect rec. #5: denormalized, read-only, synced from group.branch.
	branch = _field(schemas["Flock Group Member"], "branch")
	assert branch.get("read_only") == 1
	assert branch.get("search_index") == 1


# --------------------------------------------------------------------------- #
# Tree configuration (ADR §4.1) — Flock Branch + Flock Group are native trees
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
	"name,parent_field,target",
	[
		("Flock Branch", "parent_branch", "Flock Branch"),
		("Flock Group", "parent_group", "Flock Group"),
	],
)
def test_tree_doctypes_use_native_nested_sets(schemas, name, parent_field, target):
	doc = schemas[name]
	assert doc["is_tree"] == 1
	assert doc["nsm_parent_field"] == parent_field
	parent = _field(doc, parent_field)
	assert parent["fieldtype"] == "Link"
	assert parent["options"] == target


# --------------------------------------------------------------------------- #
# Link targets + key fields (FLO-5 §3.1/§3.2/§3.3)
# --------------------------------------------------------------------------- #


def test_flock_branch_fields(schemas):
	doc = schemas["Flock Branch"]
	assert _field(doc, "branch_name")["reqd"] == 1
	assert _field(doc, "organization")["options"] == "Flock Organization"
	for fld in ("country", "city", "timezone", "is_active", "admin_note", "parent_branch"):
		_field(doc, fld)


def test_flock_group_fields(schemas):
	doc = schemas["Flock Group"]
	assert _field(doc, "group_name")["reqd"] == 1
	assert _field(doc, "group_type")["options"] == "Flock Group Type"
	assert _field(doc, "leader")["options"] == "Flock Member"
	for fld in ("description", "is_active", "established_date", "parent_group"):
		_field(doc, fld)


def test_flock_member_fields_and_select_options(schemas):
	doc = schemas["Flock Member"]
	assert _field(doc, "full_name")["read_only"] == 1
	status = _field(doc, "status")
	assert status["fieldtype"] == "Select"
	assert status["options"].split("\n") == ["Member", "Pre-Member", "Visitor"]
	assert _field(doc, "linked_user")["options"] == "User"
	for fld in ("email", "phone", "date_joined", "gender", "dob", "is_active", "admin_note"):
		_field(doc, fld)


def test_flock_group_member_select_options(schemas):
	doc = schemas["Flock Group Member"]
	assert _field(doc, "role")["options"].split("\n") == [
		"Leader",
		"Co-Leader",
		"Member",
		"Visitor",
	]
	assert _field(doc, "status")["options"].split("\n") == ["Active", "Inactive"]
	for fld in ("group", "member", "branch", "joined_date"):
		_field(doc, fld)


def test_flock_organization_singleton_identity(schemas):
	doc = schemas["Flock Organization"]
	# ADR §3: singletons use `autoname: "FIXED"` (DB-level primary-key uniqueness
	# on `name`). `organization_name` stays as the display title field.
	assert doc["autoname"] == "FIXED"
	assert doc["title_field"] == "organization_name"
	assert _field(doc, "organization_name")["reqd"] == 1
	for fld in (
		"legal_name",
		"default_country",
		"default_timezone",
		"default_currency",
		"branding",
		"is_active",
	):
		_field(doc, fld)


def test_flock_group_type_master(schemas):
	doc = schemas["Flock Group Type"]
	assert _field(doc, "group_type_name")["reqd"] == 1
	for fld in ("description", "is_active"):
		_field(doc, fld)


# --------------------------------------------------------------------------- #
# Field-level permission markers (FLO-5 §4.4)
# --------------------------------------------------------------------------- #


def test_member_contact_fields_at_permlevel_1(schemas):
	doc = schemas["Flock Member"]
	for fld in ("email", "phone", "dob"):
		assert _field(doc, fld)["permlevel"] == 1
	assert _field(doc, "admin_note")["permlevel"] == 2


def test_doctypes_define_role_permissions(schemas):
	# Every DocType ships a DocPerm matrix incl. System Manager (usable baseline).
	roles = {"Flock Org Admin", "Flock Branch Admin", "Flock Group Leader", "Flock Auditor"}
	for name, doc in schemas.items():
		perm_roles = {p["role"] for p in doc["permissions"]}
		assert "System Manager" in perm_roles
		# The Flock roles are referenced; they materialize via the seed patch.
		assert roles & perm_roles, f"{name} has no Flock roles in its permission matrix"


# --------------------------------------------------------------------------- #
# Permission/audit infra DocTypes (FLO-20, ADR §6 / FLO-5 §5).
#
# `Flock Audit Log` is the compliance trail every privileged action appends to
# (§4.7 #2); `Flock Branch Admin Scope` drives the native branch-axis
# User-Permission subtree sync (§6.2). Both are Flock-prefixed, in flock_os,
# and carry the DocPerm matrix — including Auditor read for the audit log.
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def perm_schemas() -> dict[str, dict]:
	return {name: _load(name) for name in PERMISSION_DOCTYPES}


@pytest.mark.parametrize("name", PERMISSION_DOCTYPES)
def test_permission_doctypes_use_flock_prefix_and_module(perm_schemas, name):
	doc = perm_schemas[name]
	assert doc["doctype"] == "DocType"
	assert doc["name"] == name
	assert name.startswith("Flock ")
	assert doc["module"] == "flock_os"


def test_flock_audit_log_fields_and_scoping(perm_schemas):
	# §4.7 #2: captures action / actor / reason / detail + a nullable branch
	# (org-wide privileged actions carry no branch) + organization tenant floor.
	doc = perm_schemas["Flock Audit Log"]
	assert _field(doc, "action")["reqd"] == 1
	assert _field(doc, "reason")["reqd"] == 1
	assert _field(doc, "actor")["options"] == "User"
	assert _field(doc, "doctype_ref")["fieldtype"] == "Data"
	for fld in ("docname", "detail", "branch", "organization"):
		_field(doc, fld)
	# `branch` is nullable (org-wide events) but indexed for scoped audit reads.
	branch = _field(doc, "branch")
	assert branch["options"] == "Flock Branch"
	assert "search_index" in branch and branch["search_index"] == 1


def test_flock_audit_log_auditor_read_only(perm_schemas):
	# §4.1: Flock Auditor reads the audit log org-wide but never writes it
	# (read-only compliance role). Audit integrity = no self-serve tampering.
	doc = perm_schemas["Flock Audit Log"]
	by_role = {p["role"]: p for p in doc["permissions"]}
	assert by_role["Flock Auditor"]["read"] == 1
	assert by_role["Flock Auditor"].get("write", 0) == 0
	assert by_role["Flock Auditor"].get("delete", 0) == 0
	# Branch Admin can create audit rows (their privileged actions) but not delete.
	assert by_role["Flock Branch Admin"]["create"] == 1
	assert by_role["Flock Branch Admin"].get("delete", 0) == 0


def test_flock_branch_admin_scope_drives_up_sync(perm_schemas):
	# §6.2: one row per (user, branch admin root); carries organization floor +
	# is_active toggle. `branch` is the admin root whose subtree is materialized.
	doc = perm_schemas["Flock Branch Admin Scope"]
	user = _field(doc, "user")
	assert user["options"] == "User"
	assert user["reqd"] == 1
	branch = _field(doc, "branch")
	assert branch["options"] == "Flock Branch"
	assert branch["reqd"] == 1
	for fld in ("organization", "is_active", "last_synced_subtree"):
		_field(doc, fld)


def test_flock_branch_admin_scope_branch_admin_is_read_only(perm_schemas):
	# FLO-290 §1 (privilege-escalation fix): the Branch Admin Scope table
	# materializes a user's branch subtree into User Permission rows on
	# validate. Granting Flock Branch Admin create/write/delete here would let a
	# scoped admin widen their own (or anyone's) root → see every branch. So the
	# Branch Admin is read-only here; only Org Admin + System Manager manage
	# scopes. (Previously this asserted CRUD — that was the escalation surface.)
	doc = perm_schemas["Flock Branch Admin Scope"]
	by_role = {p["role"]: p for p in doc["permissions"] if p.get("permlevel", 0) == 0}
	assert by_role["Flock Branch Admin"].get("read") == 1
	assert by_role["Flock Branch Admin"].get("report") == 1
	for cap in ("create", "write", "delete", "share", "set_user_permissions"):
		assert by_role["Flock Branch Admin"].get(cap, 0) == 0
	# Org Admin + System Manager remain the trusted scope managers.
	assert by_role["Flock Org Admin"]["write"] == 1
	assert by_role["System Manager"]["write"] == 1


# --------------------------------------------------------------------------- #
# Bulk-attendance backing DocTypes (FLO-65, materializing the schema locked in
# `flock_os.reporting` §4.1 / FLO-10). The two tables back the queue-based bulk
# write path; the columns are pinned *exactly* to `reporting.py` so the raw-SQL
# gateway writes land on a schema that matches its assumptions. The composite
# UNIQUE indexes that gateway depends on are owned by FLO-64's
# `add_attendance_indexes` patch — the last test pins that contract tuple too.
# --------------------------------------------------------------------------- #

ATTENDANCE_DOCTYPES = (
	"Flock Attendance Record",
	"Event Attendance Summary",
)


@pytest.fixture(scope="module")
def attendance_schemas() -> dict[str, dict]:
	return {name: _load(name) for name in ATTENDANCE_DOCTYPES}


@pytest.mark.parametrize("name", ATTENDANCE_DOCTYPES)
def test_attendance_doctypes_use_flock_prefix_and_module(attendance_schemas, name):
	doc = attendance_schemas[name]
	assert doc["doctype"] == "DocType"
	assert doc["name"] == name
	assert doc["module"] == "flock_os"
	# `Event Attendance Summary` is intentionally Flock-domain even though it is
	# not `Flock `-prefixed (it is the maintained rollup table named in the
	# locked reporting schema). Both live in the flock_os module + InnoDB.
	assert doc["engine"] == "InnoDB"


def test_flock_attendance_record_columns_pinned_to_reporting(attendance_schemas):
	# FLO-65 DoD: columns pinned exactly to reporting.py §4.1
	# (event, attendee_ref, branch, status, source, client_req_id). The
	# FrappeBulkAttendanceGateway.bulk_insert writes exactly these six fields.
	doc = attendance_schemas["Flock Attendance Record"]
	fields = {f["fieldname"] for f in doc["fields"]}
	assert fields >= {
		"event",
		"attendee_ref",
		"branch",
		"status",
		"source",
		"client_req_id",
	}
	# `event`/`attendee_ref` are raw string refs (no Gathering DocType yet).
	assert _field(doc, "event")["fieldtype"] == "Data"
	assert _field(doc, "event").get("reqd") == 1
	assert _field(doc, "attendee_ref")["fieldtype"] == "Data"
	assert _field(doc, "attendee_ref").get("reqd") == 1


def test_flock_attendance_record_branch_is_permission_anchor(attendance_schemas):
	# Row-level scope rides native Frappe User Permissions on the `branch` Link
	# (ADR §6.2) — same substrate as Flock Audit Log / Flock Branch Admin Scope.
	doc = attendance_schemas["Flock Attendance Record"]
	branch = _field(doc, "branch")
	assert branch["fieldtype"] == "Link"
	assert branch["options"] == "Flock Branch"
	assert branch.get("reqd") == 1
	assert branch.get("search_index") == 1


def test_flock_attendance_record_query_indexes(attendance_schemas):
	# Hot-path query indexes for 15k-scale reads (per-event lookups,
	# idempotency probes, branch-scoped reads).
	doc = attendance_schemas["Flock Attendance Record"]
	for fld in ("event", "attendee_ref", "branch", "client_req_id"):
		assert _field(doc, fld).get("search_index") == 1


def test_event_attendance_summary_columns_and_key(attendance_schemas):
	# FLO-65 DoD: one row per (branch, event) + atomically maintained `total`.
	# The gateway's `increment_aggregate` upserts (branch, event, total) and
	# `aggregate` reads `total` — these are the only columns it touches.
	doc = attendance_schemas["Event Attendance Summary"]
	branch = _field(doc, "branch")
	assert branch["fieldtype"] == "Link"
	assert branch["options"] == "Flock Branch"
	assert branch.get("reqd") == 1
	assert branch.get("search_index") == 1
	event = _field(doc, "event")
	assert event["fieldtype"] == "Data"
	assert event.get("reqd") == 1
	assert event.get("search_index") == 1
	total = _field(doc, "total")
	assert total["fieldtype"] == "Int"
	# System-maintained counter: the field is read-only so the UI never
	# diverges the rollup from the raw-SQL write path.
	assert total.get("read_only") == 1


def test_attendance_doctypes_define_role_permissions(attendance_schemas):
	# Every DocType ships a DocPerm matrix incl. System Manager (baseline) and
	# the Flock roles that the native branch-axis User Permissions scope.
	for name, doc in attendance_schemas.items():
		perm_roles = {p["role"] for p in doc["permissions"]}
		assert "System Manager" in perm_roles
		assert {"Flock Org Admin", "Flock Branch Admin", "Flock Auditor"} & perm_roles, (
			f"{name} has no Flock roles in its permission matrix"
		)


def test_event_attendance_summary_is_read_only_for_branch_admin(attendance_schemas):
	# The rollup is system-maintained (raw upsert); Branch Admin reads it within
	# their branch scope but must not write/delete it (count integrity).
	doc = attendance_schemas["Event Attendance Summary"]
	by_role = {p["role"]: p for p in doc["permissions"]}
	assert by_role["Flock Branch Admin"]["read"] == 1
	assert by_role["Flock Branch Admin"].get("write", 0) == 0
	assert by_role["Flock Branch Admin"].get("delete", 0) == 0


def test_flock_attendance_record_branch_admin_can_report(attendance_schemas):
	# Branch Admin + Group Leader report attendance (create/write) within scope.
	doc = attendance_schemas["Flock Attendance Record"]
	by_role = {p["role"]: p for p in doc["permissions"]}
	assert by_role["Flock Branch Admin"]["create"] == 1
	assert by_role["Flock Branch Admin"]["write"] == 1
	assert by_role["Flock Group Leader"]["create"] == 1
	assert by_role["Flock Group Leader"]["write"] == 1


# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# Gathering DocTypes (FLO-54, materializing the event/gathering layer from
# [FLO-6](/FLO/issues/FLO-6) §3.1/§3.2 + ADR-0001 §3/§4.2). `Flock Gathering`
# is the canonical event entity (routine + one-time); `Flock Gathering Type` is
# its config master. Both are Flock-prefixed, in the flock_os module, and carry
# the ADR §3 scoping contract (`branch` + `group` + `organization`).
# --------------------------------------------------------------------------- #

GATHERING_DOCTYPES = (
	"Flock Gathering",
	"Flock Gathering Type",
)


@pytest.fixture(scope="module")
def gathering_schemas() -> dict[str, dict]:
	return {name: _load(name) for name in GATHERING_DOCTYPES}


@pytest.mark.parametrize("name", GATHERING_DOCTYPES)
def test_gathering_doctypes_use_flock_prefix_and_module(gathering_schemas, name):
	doc = gathering_schemas[name]
	assert doc["doctype"] == "DocType"
	assert doc["name"] == name
	assert name.startswith("Flock ")
	assert doc["module"] == "flock_os"
	assert doc["engine"] == "InnoDB"


def test_flock_gathering_is_submittable_transactional_doc(gathering_schemas):
	doc = gathering_schemas["Flock Gathering"]
	assert doc["is_submittable"] == 1
	assert doc["autoname"] == "naming_series:"
	assert doc["naming_rule"] == "By naming series"


def test_flock_gathering_scoping_contract(gathering_schemas):
	doc = gathering_schemas["Flock Gathering"]
	branch = _field(doc, "branch")
	assert branch["fieldtype"] == "Link"
	assert branch["options"] == "Flock Branch"
	assert branch.get("reqd") == 1
	assert branch.get("search_index") == 1
	group = _field(doc, "group")
	assert group["fieldtype"] == "Link"
	assert group["options"] == "Flock Group"
	assert group.get("reqd") == 1
	assert group.get("search_index") == 1
	assert _field(doc, "organization")["options"] == "Flock Organization"


def test_flock_gathering_group_path_is_denorm_rollup_helper(gathering_schemas):
	path = _field(gathering_schemas["Flock Gathering"], "group_path")
	assert path["fieldtype"] == "Data"
	assert path.get("read_only") == 1
	assert path.get("search_index") == 1


def test_flock_gathering_identity_fields(gathering_schemas):
	doc = gathering_schemas["Flock Gathering"]
	assert _field(doc, "gathering_type")["options"] == "Flock Gathering Type"
	assert _field(doc, "title").get("reqd") == 1
	starts = _field(doc, "starts_on")
	assert starts["fieldtype"] == "Datetime"
	assert starts.get("reqd") == 1
	assert starts.get("search_index") == 1
	assert _field(doc, "ends_on")["fieldtype"] == "Datetime"
	for fld in ("location", "description"):
		_field(doc, fld)
	capacity = _field(doc, "capacity")
	assert capacity["fieldtype"] == "Int"
	assert capacity.get("non_negative") == 1


def test_flock_gathering_status_select_matches_state_machine(gathering_schemas):
	from flock_os.gatherings import GATHERING_STATUSES

	status = _field(gathering_schemas["Flock Gathering"], "status")
	assert status["fieldtype"] == "Select"
	assert status.get("reqd") == 1
	assert status["options"].split("\n") == list(GATHERING_STATUSES)
	assert status["default"] == "Scheduled"


def test_flock_gathering_rollup_counters_are_permlevel_2_readonly(gathering_schemas):
	doc = gathering_schemas["Flock Gathering"]
	for fld in (
		"member_attendance_count",
		"visitor_attendance_count",
		"total_attendance_count",
		"first_time_count",
	):
		counter = _field(doc, fld)
		assert counter["fieldtype"] == "Int"
		assert counter.get("read_only") == 1
		assert counter.get("permlevel") == 2
	assert _field(doc, "reported_by").get("permlevel") == 1
	assert _field(doc, "confirmed_by").get("permlevel") == 2


def test_flock_gathering_role_permissions(gathering_schemas):
	doc = gathering_schemas["Flock Gathering"]
	# The gathering carries field-level perms (permlevel 0/1/2), so a role may
	# have several perm rows. CRUD/submit/cancel rights live on the permlevel 0
	# row — select that one explicitly rather than collapsing by role name.
	leader_pl0 = next(
		p for p in doc["permissions"] if p["role"] == "Flock Group Leader" and p.get("permlevel", 0) == 0
	)
	assert leader_pl0.get("create") == 1 and leader_pl0.get("write") == 1
	assert leader_pl0.get("submit") == 1 and leader_pl0.get("cancel") == 1
	assert leader_pl0.get("if_owner") == 1
	# Branch Admin manages (permlevel 0 row carries write).
	ba_pl0 = next(
		p for p in doc["permissions"] if p["role"] == "Flock Branch Admin" and p.get("permlevel", 0) == 0
	)
	assert ba_pl0.get("write") == 1
	by_role = {p["role"]: p for p in doc["permissions"]}
	assert by_role["Flock Member"]["read"] == 1
	assert by_role["Flock Auditor"]["read"] == 1
	assert by_role["Flock Auditor"].get("write", 0) == 0


def test_flock_gathering_type_config_master(gathering_schemas):
	doc = gathering_schemas["Flock Gathering Type"]
	assert doc["autoname"] == "field:gathering_type_name"
	assert doc["title_field"] == "gathering_type_name"
	name = _field(doc, "gathering_type_name")
	assert name.get("reqd") == 1
	assert name.get("unique") == 1
	branch = _field(doc, "branch")
	assert branch["options"] == "Flock Branch"
	assert branch.get("reqd", 0) == 0
	for fld in (
		"organization",
		"is_recurring_default",
		"default_duration_min",
		"capture_methods",
		"requires_confirmation",
		"is_active",
	):
		_field(doc, fld)


def test_flock_gathering_type_leader_read_admin_write(gathering_schemas):
	doc = gathering_schemas["Flock Gathering Type"]
	by_role = {p["role"]: p for p in doc["permissions"]}
	assert by_role["Flock Group Leader"]["read"] == 1
	assert by_role["Flock Group Leader"].get("write", 0) == 0
	assert by_role["Flock Branch Admin"]["write"] == 1
	assert by_role["Flock Auditor"]["read"] == 1


def test_flock_gathering_registered_in_scoped_doctypes(gathering_schemas):
	from flock_os.permissions import MEMBER_ANCHORED_DOCTYPES, SCOPED_DOCTYPES

	assert "Flock Gathering" in SCOPED_DOCTYPES
	assert "Flock Gathering" not in MEMBER_ANCHORED_DOCTYPES


# # Naming + composite-index contract. FLO-64's `add_attendance_indexes` patch
# materializes the composite UNIQUE indexes the bulk gateway depends on
# (FLO-10 §4.1); these tests pin (a) the autoincrement naming on the bulk
# tables and (b) that index contract against the gateway's own DocType
# constants + an independent expected set, so drift fails this gate (no bench).
# --------------------------------------------------------------------------- #

# Independent oracle for the migrate-patch's composite UNIQUE indexes. Drift
# between the patch and this set fails the gate (no missing/extra index).
EXPECTED_ATTENDANCE_INDEXES = {
	("Flock Attendance Record", ("event", "attendee_ref")),
	("Flock Attendance Record", ("event", "attendee_ref", "client_req_id")),
	("Event Attendance Summary", ("branch", "event")),
}


def _columns_of(columns_sql: str) -> tuple[str, ...]:
	return tuple(c.strip().strip("`") for c in columns_sql.strip("()").split(","))


@pytest.mark.parametrize("name", ATTENDANCE_DOCTYPES)
def test_attendance_doctypes_use_autoincrement_naming(attendance_schemas, name):
	# Append-only bulk-loaded tables use a sequence-backed surrogate PK. The
	# exact Frappe token is "autoincrement" (no underscore) — that is the string
	# `MariaDBTable.create` checks to switch `name` to `bigint` + create a
	# SEQUENCE. `autoincrement` does NOT auto-fill `name` on a raw
	# `frappe.db.bulk_insert` (Frappe gives the column no default; the SEQUENCE
	# is consumed only in the app-layer naming path, which raw SQL bypasses), so
	# the reporting gateway supplies `name` itself by drawing from the same
	# SEQUENCE (``frappe.db.get_next_sequence_val``) — pinned by
	# `test_frappe_bulk_attendance_gateway` (FLO-64). The live round-trip is
	# exercised by FLO-53's k6 scale gate.
	doc = attendance_schemas[name]
	assert doc["autoname"] == "autoincrement"
	assert doc["naming_rule"] == "Autoincrement"


def test_composite_unique_index_contract_matches_gateway(attendance_schemas):
	"""The migrate patch's INDEXES must target the gateway's exact DocTypes and
	reference real columns; the (doctype, columns) set must match the expected
	contract exactly (no missing/extra composite unique index)."""
	from flock_os.patches.v0_1.add_attendance_indexes import INDEXES
	from flock_os.reporting import FrappeBulkAttendanceGateway

	gateway_doctypes = {
		FrappeBulkAttendanceGateway.ATTENDANCE_DOCTYPE,
		FrappeBulkAttendanceGateway.SUMMARY_DOCTYPE,
	}

	declared = set()
	for doctype, _index_name, columns_sql in INDEXES:
		# Each index targets a DocType the gateway actually writes/reads.
		assert doctype in gateway_doctypes, f"patch index targets unknown doctype {doctype!r}"
		assert doctype in attendance_schemas, f"missing schema for {doctype!r}"
		columns = _columns_of(columns_sql)
		declared.add((doctype, columns))
		# Each index column is a real field on that DocType (else the patch
		# would fail on `bench migrate`).
		field_names = {f["fieldname"] for f in attendance_schemas[doctype]["fields"]}
		for col in columns:
			assert col in field_names, f"{doctype}: index column {col!r} is not a field"

	assert declared == EXPECTED_ATTENDANCE_INDEXES
