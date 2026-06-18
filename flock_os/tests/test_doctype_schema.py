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


def test_flock_branch_admin_scope_branch_admin_can_manage(perm_schemas):
	# A Branch Admin manages their own scope rows (CRUD) — the self-service path
	# for the native branch-axis UP sync.
	doc = perm_schemas["Flock Branch Admin Scope"]
	by_role = {p["role"]: p for p in doc["permissions"]}
	assert by_role["Flock Branch Admin"]["create"] == 1
	assert by_role["Flock Branch Admin"]["write"] == 1
	assert by_role["Flock Branch Admin"]["delete"] == 1
