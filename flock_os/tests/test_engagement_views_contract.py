"""
Project-level tests for the FLO-190 facilitator launch + authoring surface.

These run under plain ``pytest`` (no Frappe site / bench / Redis required). They
pin two concerns:

* **Pure reconciliation helpers** (:mod:`flock_os.engagement`) — the frappe-free
  logic that bridges the FLO-12 portal/JS contract to the FLO-11 runtime
  transport: room-code generation, the client ``session``/``name``/``room_code``
  ref normalization, the inline-config → ``config`` pack, the player
  ``{kind, payload}`` envelope unpack, and the template → launch-config
  projection. These carry every branch of logic so they stay unit-tested; the
  ``@frappe.whitelist()`` adapter (:mod:`flock_os.engagement_views`) is a thin
  bench-exercised wrapper (omitted from the coverage ratchet).
* **The engagement_views contract** — FLO-12's ``ENGAGEMENT_ENDPOINTS`` pointed
  at ``flock_os.engagement_views.*`` but the module never existed (false-green
  gate: the old test only checked the string prefix). FLO-190 ships the module,
  and these tests statically verify every documented endpoint resolves to a real
  whitelisted function, parsing the adapter source with :mod:`ast` so no bench is
  required.
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

from flock_os import engagement as eng

VIEWS_PATH = pathlib.Path(eng.__file__).parent / "engagement_views.py"


# --------------------------------------------------------------------------- #
# Room code (FLO-9 §2 — 6-digit join code).
# --------------------------------------------------------------------------- #
def test_generate_room_code_is_six_digits():
	code = eng.generate_room_code()
	assert re.fullmatch(r"\d{6}", code), code


def test_generate_room_code_is_deterministic_with_seeded_rng():
	import random

	rng_a = random.Random(1234)
	rng_b = random.Random(1234)
	assert eng.generate_room_code(rng_a) == eng.generate_room_code(rng_b)


def test_generate_room_code_varies_across_calls():
	import random

	rng = random.Random(99)
	codes = {eng.generate_room_code(rng) for _ in range(20)}
	# Practically certain to produce >1 distinct code across 20 draws.
	assert len(codes) > 1


# --------------------------------------------------------------------------- #
# Session ref normalization (the client's many spellings of "which session").
# --------------------------------------------------------------------------- #
def test_resolve_session_ref_prefers_direct_id():
	assert eng.resolve_session_ref({"session_id": "ENG-1"}) == {"session_id": "ENG-1"}


def test_resolve_session_ref_accepts_console_aliases():
	assert eng.resolve_session_ref({"name": "ENG-2"}) == {"session_id": "ENG-2"}
	assert eng.resolve_session_ref({"session": "ENG-3"}) == {"session_id": "ENG-3"}


def test_resolve_session_ref_falls_back_to_room_code():
	assert eng.resolve_session_ref({"room_code": "123456"}) == {"room_code": "123456"}


def test_resolve_session_ref_raises_when_nothing_supplied():
	with pytest.raises(eng.FlockEngagementError):
		eng.resolve_session_ref({})


def test_resolve_session_ref_id_beats_room_code():
	# A direct id wins so a stale code on the request can't hijack the session.
	assert eng.resolve_session_ref({"session_id": "ENG-1", "room_code": "000000"}) == {"session_id": "ENG-1"}


# --------------------------------------------------------------------------- #
# Session config packing (flat console fields → runtime config dict).
# --------------------------------------------------------------------------- #
def test_pack_session_config_folds_flat_fields():
	config = eng.pack_session_config({"rounds": 3, "calm_default": True})
	assert config == {"rounds": 3, "calm_default": True}


def test_pack_session_config_preserves_nested_config():
	config = eng.pack_session_config({"config": {"options": ["a", "b"]}, "rounds": 2})
	assert config["options"] == ["a", "b"]
	assert config["rounds"] == 2


def test_pack_session_config_ignores_missing_fields():
	assert eng.pack_session_config({}) == {}


def test_pack_session_config_does_not_write_none():
	# ``rounds=None`` (console sends null) must not clobber a template default.
	assert eng.pack_session_config({"rounds": None, "config": {"rounds": 5}}) == {"rounds": 5}


# --------------------------------------------------------------------------- #
# Participate payload unpack (player {kind, payload} → engagement_api fields).
# --------------------------------------------------------------------------- #
def test_unpack_tap_burst_records_score():
	out = eng.unpack_participate_payload("tap_burst", {"round": 1, "hit": 7})
	assert out["score"] == 7.0
	assert out["feedback"]["hit"] == 7


def test_unpack_preserves_full_payload_as_feedback_for_every_kind():
	for kind in ("quiz_race", "reaction", "bingo", "team_challenge", "poll", "word_cloud", "qa", "pulse"):
		payload = {"any": kind, "nested": {"x": 1}}
		out = eng.unpack_participate_payload(kind, payload)
		assert out["feedback"] == payload
		# Non-tap kinds don't synthesize a score (attendance still credits).
		assert "score" not in out


def test_unpack_handles_empty_payload():
	assert eng.unpack_participate_payload("poll", {}) == {"feedback": {}}


# --------------------------------------------------------------------------- #
# Template → launch config projection (FLO-190 authoring + launch wiring).
# --------------------------------------------------------------------------- #
def test_template_doctype_for_kind_splits_families():
	assert eng.template_doctype_for_kind("poll") == "Flock Engagement Questionnaire Template"
	assert eng.template_doctype_for_kind("tap_burst") == "Flock Engagement Game Template"


def test_template_doctype_for_kind_rejects_unknown():
	with pytest.raises(eng.FlockEngagementError):
		eng.template_doctype_for_kind("nope")


def test_template_to_launch_config_for_questionnaire():
	tpl = {
		"name": "Q-TPL-1",
		"doctype": "Flock Engagement Questionnaire Template",
		"template_name": "Sunday Pulse",
		"kind": "pulse",
		"config": '{"sliders":[{"key":"mood"}]}',
		"accessibility_mode_default": 1,
	}
	out = eng.template_to_launch_config(tpl)
	assert out["kind"] == "pulse"
	assert out["engagement_type"] == "questionnaire"
	assert out["title"] == "Sunday Pulse"
	assert out["config"] == {"sliders": [{"key": "mood"}]}
	assert out["accessibility_mode_default"] is True
	assert out["template_name"] == "Q-TPL-1"


def test_template_to_launch_config_for_game_with_dict_config():
	tpl = {
		"name": "G-1",
		"kind": "bingo",
		"config": {"actions": ["a", "b"]},
		"template_name": "Welcome Bingo",
	}
	out = eng.template_to_launch_config(tpl)
	assert out["engagement_type"] == "game"
	assert out["config"] == {"actions": ["a", "b"]}


def test_template_to_launch_config_handles_invalid_json_config():
	out = eng.template_to_launch_config({"kind": "poll", "config": "not-json"})
	assert out["config"] == {}


def test_template_to_launch_config_requires_kind():
	with pytest.raises(eng.FlockEngagementError):
		eng.template_to_launch_config({"template_name": "x"})


def test_template_summary_projects_lean_shape():
	row = {
		"name": "Q-TPL-2",
		"template_name": "Midweek Poll",
		"kind": "poll",
		"description": "Quick check-in",
		"is_active": 1,
		"reviewed": 0,
		"accessibility_mode_default": 0,
	}
	s = eng.template_summary(row)
	assert s["name"] == "Q-TPL-2"
	assert s["engagement_type"] == "questionnaire"
	assert s["doctype"] == "Flock Engagement Questionnaire Template"
	assert s["is_active"] is True
	assert s["reviewed"] is False


# --------------------------------------------------------------------------- #
# ENGAGEMENT_VIEWS_CONTRACT — every documented endpoint routes somewhere.
# --------------------------------------------------------------------------- #
def test_every_endpoint_has_a_view_in_the_contract():
	# Closes the FLO-12 gap: ENGAGEMENT_ENDPOINTS used to point at a module that
	# did not exist. The contract now pins one declared view per endpoint key.
	missing = set(eng.ENGAGEMENT_ENDPOINTS) - set(eng.ENGAGEMENT_VIEWS_CONTRACT)
	assert not missing, f"endpoints without a declared view: {sorted(missing)}"


def test_contract_includes_the_template_authoring_surface():
	# FLO-190's unique deliverable: template list + get for the launch picker.
	assert "list_templates" in eng.ENGAGEMENT_VIEWS_CONTRACT
	assert "get_template" in eng.ENGAGEMENT_VIEWS_CONTRACT


def test_contract_view_names_are_valid_identifiers():
	for key, fn_name in eng.ENGAGEMENT_VIEWS_CONTRACT.items():
		assert re.fullmatch(r"[a-z_][a-z0-9_]*", fn_name), (key, fn_name)


def test_template_author_roles_exclude_group_leader():
	# DocType perms deny Group Leaders write on templates; the authoring UI
	# mirrors that so a leader sees a read-only launch list, not a create button.
	assert eng.ROLE_ORG_ADMIN in eng.TEMPLATE_AUTHOR_ROLES
	assert eng.ROLE_BRANCH_ADMIN in eng.TEMPLATE_AUTHOR_ROLES
	assert eng.ROLE_GROUP_LEADER not in eng.TEMPLATE_AUTHOR_ROLES


# --------------------------------------------------------------------------- #
# Static contract closure — every declared view is a real @frappe.whitelist()
# function in engagement_views.py (parsed with ast, so no bench required).
# This is the direct fix for the FLO-58/FLO-103 false-green class.
# --------------------------------------------------------------------------- #
def _parse_views():
	return ast.parse(VIEWS_PATH.read_text())


def _decorated_whitelist_fns(tree):
	"""Return the set of top-level function names decorated ``@frappe.whitelist()``."""
	out = set()
	for node in tree.body:
		if not isinstance(node, ast.FunctionDef):
			continue
		for dec in node.decorator_list:
			target = dec.func if isinstance(dec, ast.Call) else dec
			if isinstance(target, ast.Attribute) and target.attr == "whitelist":
				out.add(node.name)
			elif isinstance(target, ast.Name) and target.id == "whitelist":
				out.add(node.name)
	return out


def test_engagement_views_module_exists():
	assert VIEWS_PATH.is_file(), "flock_os/engagement_views.py is missing (FLO-190)"


def test_every_contract_view_is_a_real_whitelisted_function():
	tree = _parse_views()
	whitelisted = _decorated_whitelist_fns(tree)
	declared = set(eng.ENGAGEMENT_VIEWS_CONTRACT.values())
	missing = declared - whitelisted
	assert not missing, f"declared views not whitelisted in engagement_views.py: {sorted(missing)}"


def test_endpoints_route_into_the_real_views_module():
	# Every endpoint path the client calls is flock_os.engagement_views.<fn>,
	# and <fn> is now a real declared view (not just a string prefix).
	tree = _parse_views()
	whitelisted = _decorated_whitelist_fns(tree)
	for key, path in eng.ENGAGEMENT_ENDPOINTS.items():
		assert path.startswith("flock_os.engagement_views."), (key, path)
		fn = path.split(".", 2)[-1]
		assert fn in whitelisted, f"{key} -> {fn} is not a real whitelisted view"


def test_engagement_views_imports_the_runtime_transport():
	# The adapter must delegate to FLO-11's engagement_api (no reimplementation).
	src = VIEWS_PATH.read_text()
	tree = ast.parse(src)
	imports = {
		alias.asname or alias.name
		for node in ast.walk(tree)
		if isinstance(node, ast.ImportFrom)
		for alias in node.names
	}
	assert "engagement_api" in imports, "engagement_views must import flock_os.engagement_api"
