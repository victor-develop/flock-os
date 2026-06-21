"""
Project-level tests for the Fun Attendance frontend contract (FLO-12 / FLO-9 §12).

Run under plain ``pytest`` (no Frappe site / Redis / bench), mirroring the
SQL-light hexagonal pattern from ``test_portal`` / ``test_notifications``. They
pin the FLO-12 Definition of Done for the *pure* frontend-contract layer
(:mod:`flock_os.engagement`):

* **Kind catalog** — the 9 starter kinds + families + components + scoring + the
  Calm Check-in guarantee for every timed game (FLO-9 §3 / §7).
* **Accessibility config** — the a11y profile merges over defaults + the JS
  parity constants the browser must replicate.
* **JS shard parity** — the documented crc32 shard algorithm matches Python's
  ``zlib.crc32`` so a player lands on the same shard the server fans out to
  (ADR §5.1). This is the cross-language contract that keeps realtime working.
* **Facilitator console context** — the no-leakage scope set a facilitator may
  host engagement for (mirrors ``test_portal`` compose-context guarantees), and
  the don't-trust-the-client target guard.

The client JS itself is exercised via the injected JSON contract; the portal
pages + Frappe adapter are bench-integration surface (out of scope here).
"""

from __future__ import annotations

import zlib

import pytest

from flock_os import engagement as eng
from flock_os import realtime as rt

ORG = "ORG"

# --------------------------------------------------------------------------- #
# In-memory world — same two-tree shape as test_portal (ADR §4).
#
#   HQ ─── North ─── North-A   (gatherings: G-NorthA-Sun, G-NorthA-Youth)
#          South              (gathering:   G-South-Sun)
#
# alice = Org Admin (global — sees every branch in ORG).
# bob   = Branch Admin scoped to the North subtree (UP rows: North, North-A).
# carol = Group Leader scoped to North-A.
# dave  = Member (no facilitator role).
# --------------------------------------------------------------------------- #
BRANCH_PARENT = {"HQ": None, "North": "HQ", "North-A": "North", "South": "HQ"}
GATHERINGS = (
	{"name": "G-NorthA-Sun", "branch": "North-A", "label": "North-A Sunday"},
	{"name": "G-NorthA-Youth", "branch": "North-A", "label": "North-A Youth"},
	{"name": "G-South-Sun", "branch": "South", "label": "South Sunday"},
)
GROUPS = (
	{"name": "GR-NorthA", "branch": "North-A", "label": "North-A Group"},
	{"name": "GR-South", "branch": "South", "label": "South Group"},
)
SCOPES = {
	"alice": frozenset({eng.ROLE_ORG_ADMIN}),
	"bob": frozenset({eng.ROLE_BRANCH_ADMIN}),
	"carol": frozenset({eng.ROLE_GROUP_LEADER}),
	"dave": frozenset(),
}


class MemGateway(eng.FacilitatorGateway):
	"""In-memory facilitator gateway (no Frappe) — the no-leakage boundary."""

	def __init__(self) -> None:
		self._up = {  # user -> materialized branch subtree (ADR §6.2)
			"alice": ("HQ", "North", "North-A", "South"),
			"bob": ("North", "North-A"),
			"carol": ("North-A",),
			"dave": (),
		}

	def get_user_roles(self, user: str) -> tuple[str, ...]:
		return tuple(SCOPES.get(user, frozenset()))

	def get_user_organization(self, user: str) -> str | None:
		return ORG if user in SCOPES else None

	def facilitator_branches(self, user: str) -> tuple[str, ...]:
		return self._up.get(user, ())

	def branch_label(self, branch: str) -> str | None:
		return branch

	def gatherings_for_branches(self, branches: tuple[str, ...]) -> tuple[dict, ...]:
		allowed = set(branches)
		return tuple(g for g in GATHERINGS if g["branch"] in allowed)

	def groups_for_branches(self, branches: tuple[str, ...]) -> tuple[dict, ...]:
		allowed = set(branches)
		return tuple(g for g in GROUPS if g["branch"] in allowed)


@pytest.fixture()
def gw() -> MemGateway:
	return MemGateway()


# --------------------------------------------------------------------------- #
# Kind catalog (FLO-9 §3)
# --------------------------------------------------------------------------- #
def test_catalog_has_the_nine_starter_kinds():
	kinds = {k.kind for k in eng.ENGAGEMENT_CATALOG}
	assert kinds == {
		eng.KIND_TAP_BURST,
		eng.KIND_QUIZ_RACE,
		eng.KIND_REACTION,
		eng.KIND_BINGO,
		eng.KIND_TEAM_CHALLENGE,
		eng.KIND_POLL,
		eng.KIND_WORD_CLOUD,
		eng.KIND_QA,
		eng.KIND_PULSE,
	}


def test_each_kind_has_a_component_tag_matching_the_design_handoff():
	# FLO-9 §12 names the exact component tags the router mounts.
	assert eng.COMPONENT_BY_KIND == {
		eng.KIND_TAP_BURST: "TapBurst",
		eng.KIND_QUIZ_RACE: "QuizRace",
		eng.KIND_REACTION: "ReactionTap",
		eng.KIND_BINGO: "BingoCard",
		eng.KIND_TEAM_CHALLENGE: "TeamChallenge",
		eng.KIND_POLL: "LivePoll",
		eng.KIND_WORD_CLOUD: "WordCloud",
		eng.KIND_QA: "LiveQA",
		eng.KIND_PULSE: "PulseSurvey",
	}


def test_every_timed_game_offers_calm_check_in():
	# FLO-9 §7: "every timed game has a calm equivalent" — attendance credits
	# without scoring reaction time. Pure questionnaires are untimed.
	timed = {k.kind for k in eng.ENGAGEMENT_CATALOG if k.timed}
	assert timed == {eng.KIND_TAP_BURST, eng.KIND_QUIZ_RACE, eng.KIND_REACTION}
	for kind in eng.ENGAGEMENT_CATALOG:
		if kind.timed:
			assert kind.calm_checkin, f"{kind.kind} is timed but has no Calm Check-in"
	# Calm-checkin set == exactly the timed games.
	assert eng.CALM_CHECKIN_KINDS == timed


def test_games_are_scored_and_questionnaires_are_not():
	scored = {k.kind for k in eng.ENGAGEMENT_CATALOG if k.scored}
	assert scored == {
		eng.KIND_TAP_BURST,
		eng.KIND_QUIZ_RACE,
		eng.KIND_REACTION,
		eng.KIND_BINGO,
		eng.KIND_TEAM_CHALLENGE,
	}
	questionnaires = {k.kind for k in eng.ENGAGEMENT_CATALOG if k.family == eng.FAMILY_QUESTIONNAIRE}
	assert questionnaires == {eng.KIND_POLL, eng.KIND_WORD_CLOUD, eng.KIND_QA, eng.KIND_PULSE}
	assert scored.isdisjoint(questionnaires)


def test_catalog_json_round_trips_and_get_kind_resolves():
	rows = eng.catalog_json()
	assert {r["kind"] for r in rows} == {k.kind for k in eng.ENGAGEMENT_CATALOG}
	assert eng.get_kind(eng.KIND_POLL) is not None
	assert eng.get_kind("nope") is None
	# valid_kinds is the engagement_kind select-option order.
	assert tuple(r["kind"] for r in rows) == eng.valid_kinds()


# --------------------------------------------------------------------------- #
# Accessibility config (FLO-9 §7, WCAG 2.1 AA)
# --------------------------------------------------------------------------- #
def test_min_target_meets_wcag_aa():
	# WCAG 2.1 AA target size is ~48x48 CSS px (2.5.5 enhanced is 44 in 2.1 AA
	# but the design commits to >=48).
	assert eng.A11Y_MIN_TARGET_PX >= 48


def test_resolve_a11y_profile_merges_and_drops_unknown_keys():
	merged = eng.resolve_a11y_profile({"reduced_motion": True, "bogus": True})
	assert merged["reduced_motion"] is True
	assert merged["enabled"] is False  # default retained
	assert "bogus" not in merged
	# None -> defaults verbatim.
	assert eng.resolve_a11y_profile(None) == eng.DEFAULT_A11Y_PROFILE


# --------------------------------------------------------------------------- #
# JS parity contract (ADR §5.1 / FLO-10 §5.1) — the cross-language shard rule.
# --------------------------------------------------------------------------- #
def test_parity_contract_matches_realtime_module():
	contract = eng.js_parity_contract()
	assert contract["shard_count"] == rt.DEFAULT_SHARD_COUNT
	ev = contract["realtime_events"]
	assert ev["game_state"] == rt.RT_GAME_STATE
	assert ev["attendance_presence"] == rt.RT_ATTENDANCE_PRESENCE
	assert ev["attendance_count"] == rt.RT_ATTENDANCE_COUNT
	# Channel naming must round-trip realtime.broadcast_channel / shard_channel.
	assert contract["broadcast_channel"] == "flock_os:event:<session_id>:broadcast"
	assert contract["shard_channel"] == "flock_os:event:<session_id>:shard:<n>"
	for session in ("S1", "engagement-session-9"):
		assert rt.broadcast_channel(session).startswith(rt.EVENT_ROOM_PREFIX)
		assert rt.shard_channel(session, 3).startswith(rt.EVENT_ROOM_PREFIX)


def _js_crc32(text: str) -> int:
	"""Re-implementation of the EXACT crc32 algorithm in engagement-core.js.

	Polynomial 0xedb88320, table-based, `>>> 0` to unsigned. Used only to pin
	the parity contract — the browser must produce the same value as
	``flock_os.realtime.shard_for``.
	"""
	table = []
	for n in range(256):
		c = n
		for _ in range(8):
			c = (0xEDB88320 ^ (c >> 1)) if (c & 1) else (c >> 1)
		table.append(c & 0xFFFFFFFF)
	crc = 0xFFFFFFFF
	for b in text.encode("utf-8"):
		crc = table[(crc ^ b) & 0xFF] ^ (crc >> 8)
	return (crc ^ 0xFFFFFFFF) & 0xFFFFFFFF


@pytest.mark.parametrize(
	"ref",
	[
		"member-001",
		"visitor-device-abc",
		"S::attendee-7",
		"",
		"ünïcödé-é",
		"x" * 200,
	],
)
def test_js_crc32_matches_python_zlib_the_shard_parity_contract(ref):
	# The browser crc32 must equal zlib.crc32 so shard assignment agrees.
	assert _js_crc32(ref) == zlib.crc32(ref.encode("utf-8"))
	# And therefore the shard (crc32 % N) matches realtime.shard_for.
	N = rt.DEFAULT_SHARD_COUNT
	assert (_js_crc32(ref) % N) == rt.shard_for(ref, N)


def test_shard_assignment_is_stable_and_within_range():
	N = rt.DEFAULT_SHARD_COUNT
	for ref in ("a", "b", "c", "member-42"):
		s = rt.shard_for(ref, N)
		assert 0 <= s < N


# --------------------------------------------------------------------------- #
# Facilitator console context — no-leakage (mirrors test_portal FLO-60 DoD).
# --------------------------------------------------------------------------- #
def test_is_facilitator_role_gate():
	assert eng.is_facilitator(SCOPES["alice"])
	assert eng.is_facilitator(SCOPES["bob"])
	assert eng.is_facilitator(SCOPES["carol"])
	assert not eng.is_facilitator(SCOPES["dave"])


def test_build_facilitator_context_confined_to_subtree(gw: MemGateway):
	ctx = eng.build_facilitator_context(user="bob", gateway=gw)
	# bob = North subtree (North, North-A) — siblings (South) never appear.
	assert {b["name"] for b in ctx["branches"]} == {"North", "North-A"}
	assert "South" not in {b["name"] for b in ctx["branches"]}
	# Gatherings + groups confined to the same subtree.
	assert {g["branch"] for g in ctx["gatherings"]} <= {"North", "North-A"}
	assert {g["branch"] for g in ctx["groups"]} <= {"North", "North-A"}
	# The contract payload is injected for the client.
	assert len(ctx["kinds"]) == 9
	assert ctx["parity"]["shard_count"] == rt.DEFAULT_SHARD_COUNT
	assert "create_session" in ctx["endpoints"]
	assert ctx["is_facilitator"] is True


def test_build_facilitator_context_rejects_non_facilitator(gw: MemGateway):
	with pytest.raises(eng.FlockEngagementError):
		eng.build_facilitator_context(user="dave", gateway=gw)


def test_assert_host_target_rejects_cross_subtree(gw: MemGateway):
	ctx = eng.build_facilitator_context(user="carol", gateway=gw)
	# carol = North-A only. North-A is offered; South is a sibling.
	eng.assert_host_target_in_context(
		branch="North-A", group="GR-NorthA", gathering="G-NorthA-Sun", context=ctx
	)
	with pytest.raises(eng.FlockEngagementError):
		eng.assert_host_target_in_context(branch="South", group=None, gathering=None, context=ctx)
	with pytest.raises(eng.FlockEngagementError):
		# A forged cross-subtree gathering under a valid branch.
		eng.assert_host_target_in_context(branch="North-A", group=None, gathering="G-South-Sun", context=ctx)
	with pytest.raises(eng.FlockEngagementError):
		eng.assert_host_target_in_context(branch=None, group=None, gathering=None, context=ctx)


# --------------------------------------------------------------------------- #
# API surface — every documented FLO-9 §11 route has an endpoint mapping.
# --------------------------------------------------------------------------- #
def test_endpoints_cover_the_design_api_surface():
	expected = {
		"create_session",
		"open_session",
		"close_session",
		"join_session",
		"participate",
		"session_state",
		"flush_offline",
		"facilitator_context",
		"review_queue",
		"manual_override",
	}
	assert expected <= set(eng.ENGAGEMENT_ENDPOINTS)
	# Every endpoint is a frappe.call method path the client uses.
	for path in eng.ENGAGEMENT_ENDPOINTS.values():
		assert path.startswith("flock_os.engagement_views.")


# --------------------------------------------------------------------------- #
# SEC-XSS-1 (FLO-685) — engage-host.js status line must HTML-escape exception
# text. setStatus renders via innerHTML, so any ``e.message`` concatenated into
# an error status would otherwise execute injected markup. This static guard
# pins the escape so a future caller cannot regress it.
# --------------------------------------------------------------------------- #
from pathlib import Path as _Path  # noqa: E402

_ENGAGE_HOST_JS = _Path(__file__).resolve().parent.parent / "public" / "js" / "engage-host.js"


def _engage_host_source() -> str:
	return _ENGAGE_HOST_JS.read_text(encoding="utf-8")


def test_engage_host_defines_escape_html_helper():
	src = _engage_host_source()
	# The helper must exist and prefer Frappe's own escaper.
	assert "escape_html" in src
	assert "frappe.utils.escape_html" in src


@pytest.mark.parametrize(
	"needle",
	[
		"Could not load that template.",
		"Could not create session.",
		"Could not open.",
		"Could not close.",
		"Override failed.",
	],
)
def test_engage_host_error_status_lines_escape_exception_text(needle):
	import re

	src = _engage_host_source()
	# Each error setStatus line that references e.message must wrap it in
	# escape_html(...) — never concatenate the raw value.
	matches = [ln for ln in src.splitlines() if 'setStatus("error"' in ln and needle in ln]
	assert matches, f"expected a setStatus error line mentioning {needle!r}"
	for ln in matches:
		assert "e.message" not in ln or "escape_html(" in ln, (
			f"raw e.message in setStatus error line (XSS): {ln.strip()!r}"
		)
		# And the unescaped concatenation pattern must be gone.
		assert not re.search(r"\+ \(\(e && e\.message\)", ln), (
			f"unescaped e.message concatenation remains: {ln.strip()!r}"
		)


def test_engage_host_escape_html_fallback_neutralizes_markup():
	# The self-contained fallback escaper (used when frappe.utils isn't loaded
	# yet) must neutralize the characters that break out of a text node /
	# attribute. Mirrors the replace chain in engage-host.js.
	def fallback(s: str) -> str:
		return (
			str(s)
			.replace("&", "&amp;")
			.replace("<", "&lt;")
			.replace(">", "&gt;")
			.replace('"', "&quot;")
			.replace("'", "&#39;")
		)

	payload = '<img src=x onerror="alert(1)">&\'"'
	out = fallback(payload)
	# Escaping neutralizes the markup delimiters — the tag/attribute structure
	# can no longer form, so the payload renders as inert visible text. (The
	# word "onerror" may remain as harmless text content; that's expected.)
	assert "<" not in out and ">" not in out
	assert '"' not in out and "'" not in out
	assert "&lt;img" in out and "&gt;" in out
