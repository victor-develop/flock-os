"""
Agent-liveness watchdog runner lock (FLO-968).

Pins the deterministic runner that executes the agent-liveness recovery
runbook (``docs/operations/agent-liveness-recovery-runbook.md``) end-to-end:
detect → release → restart (exp backoff, max 3) → escalate to the board. The
[FLO-968](/FLO/issues/FLO-968) CEO watchdog fire failed mid-run because the
recovery-owner agent hand-executed the procedure with curl; this runner is the
single entry point that makes the loop deterministic. The decision math it
obeys is already pinned by ``test_recovery.py`` (FLO-395); this suite pins the
*orchestration* — the loop, the side effects, the suppressions, and the exit
posture — against scripted probes + a recording action sink.

Pure logic: no bench, no Frappe, no network. ``run_watchdog`` is exercised
with :class:`ScriptedLivenessReader` + :class:`RecordingRecoveryActions`, a
fixed clock (``now_epoch``), and a no-op sleep so the full retry loop runs
instantly. The canonical negative-test shapes are covered:

* the FLO-365 incident shape (stuck run → release+invoke recovers, owner is the
  manager / configured peer — never the stuck agent);
* exhaustion (3 attempts fail → board approval, never a 4th attempt);
* the FLO-419 zombie + FLO-771 missing-disposition suppressions;
* the release-403 fast-path to escalation.

The CLI's ``--self-test`` mirrors these cases; this pytest suite is the
canonical CI gate (it runs under ``git ls-files 'tools/**/tests/test_*.py'``).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from tools.ops.agent_liveness.recovery import (
	AgentNode,
	ChainOfCommand,
)
from tools.ops.agent_liveness.watchdog import (
	ACTION_DRY_RUN,
	ACTION_ESCALATE_BOARD,
	ACTION_NOOP,
	ACTION_RECOVERED,
	LivenessProbe,
	PaperclipLivenessReader,
	RecordingRecoveryActions,
	ScriptedLivenessReader,
	WatchdogConfig,
	run_watchdog,
)
from tools.ops.ceo_heartbeat.monitor import HeartbeatRun

# ---------------------------------------------------------------------------- #
# Fixtures — the Flock OS chain of command + a simulated stuck CEO run.
# ---------------------------------------------------------------------------- #
# The CEO is the top of the chain; the Software Architect is the configured
# peer that owns the CEO watchdog routine. When the CEO is stuck, recovery
# must route to the Architect (never the CEO) and the board is the backstop.
CEO_ID = "d572935c-f075-471f-aab8-0bd2d9a975ba"
ARCHITECT_ID = "f893dff0-4d7c-4d24-9234-a8d5de384fa1"
DEVOPS_ID = "e35458ca-b563-4394-a9d7-e73c28fd6b77"

CHAIN = ChainOfCommand.from_agents(
	[
		AgentNode(id=CEO_ID, name="CEO", reports_to=None),
		AgentNode(id=ARCHITECT_ID, name="SoftwareArchitect", reports_to=CEO_ID),
		AgentNode(id=DEVOPS_ID, name="DevOpsEngineer", reports_to=ARCHITECT_ID),
	]
)

NOW = 1_800_000_000.0  # arbitrary fixed clock for deterministic age math
WATCHDOG_ISSUE = "watchdog-run-issue"
LINKED_ISSUE = "83a87e75-0000-0000-0000-000000000968"


def _cfg(watched=CEO_ID, fallback=ARCHITECT_ID, **kw) -> WatchdogConfig:
	base = dict(
		watched_agent_id=watched,
		watched_routine_id="routine-ceo",
		ceo_fallback_owner_id=fallback,
		watchdog_issue_id=WATCHDOG_ISSUE,
	)
	base.update(kw)
	return WatchdogConfig(**base)


def _run(
	age_minutes: float,
	*,
	run_id: str = "run-1",
	status: str = "issue_created",
	linked: str = LINKED_ISSUE,
	coalesced: str | None = None,
) -> HeartbeatRun:
	return HeartbeatRun(
		id=run_id,
		status=status,
		triggered_at_epoch=NOW - age_minutes * 60,
		completed_at_epoch=None,
		linked_issue_id=linked,
		coalesced_into_run_id=coalesced,
	)


def _probe(
	run: HeartbeatRun | None,
	*,
	all_runs=(),
	chain=CHAIN,
	terminal=False,
	missing_disposition=False,
) -> LivenessProbe:
	runs = tuple(all_runs) if all_runs else ((run,) if run is not None else ())
	return LivenessProbe(
		run=run,
		all_runs=runs,
		chain=chain,
		linked_issue_is_terminal=terminal,
		successful_run_missing_disposition=missing_disposition,
	)


def _run_watchdog(config, probes, actions=None, **kw):
	"""Drive ``run_watchdog`` with scripted probes + a recording sink (no sleep)."""
	actions = actions or RecordingRecoveryActions()
	outcome = run_watchdog(
		config,
		ScriptedLivenessReader(list(probes)),
		actions,
		now_epoch=NOW,
		sleep=lambda _s: None,
		**kw,
	)
	return outcome, actions


# --------------------------------------------------------------------------- #
# Healthy / suspicious (Step 1–2: NOOP)
# --------------------------------------------------------------------------- #
def test_no_in_flight_run_is_healthy_noop():
	outcome, actions = _run_watchdog(_cfg(), [_probe(None)])
	assert outcome.action == ACTION_NOOP
	assert outcome.attempts_made == 0
	# Healthy with no run posts no mutating comment-path action.
	assert actions.releases == []
	assert actions.invokes == []


def test_young_run_within_bounds_is_healthy():
	outcome, _ = _run_watchdog(_cfg(), [_probe(_run(5))])
	assert outcome.action == ACTION_NOOP
	assert outcome.verdict.verdict == "healthy"


def test_linked_issue_terminal_is_healthy_even_when_old():
	# A 40m-old run whose linked issue is done is NOT stuck — the run finished.
	outcome, actions = _run_watchdog(_cfg(), [_probe(_run(40), terminal=True)])
	assert outcome.action == ACTION_NOOP
	assert actions.releases == []


def test_suspicious_run_watches_without_recovering():
	outcome, actions = _run_watchdog(_cfg(), [_probe(_run(16))])  # 15 <= 16 < 20
	assert outcome.action == ACTION_NOOP
	assert outcome.verdict.verdict == "suspicious"
	# Suspicious posts a watch comment but never releases.
	assert len(actions.comments) == 1
	assert actions.releases == []
	assert actions.invokes == []


# --------------------------------------------------------------------------- #
# Recovery loop (Step 3): release + invoke, recover on re-detect
# --------------------------------------------------------------------------- #
def test_stuck_run_recovers_on_first_attempt():
	stuck = _probe(_run(25))  # >= T_STUCK(20), < T_ABORT(45)
	healthy = _probe(None)  # post-invoke: run cleared
	outcome, actions = _run_watchdog(_cfg(), [stuck, healthy])
	assert outcome.action == ACTION_RECOVERED
	assert outcome.attempts_made == 1
	assert actions.releases == [(LINKED_ISSUE, ARCHITECT_ID)]
	assert actions.invokes == [CEO_ID]
	# A recovery comment is posted on the watchdog run issue.
	assert actions.comments[0][0] == WATCHDOG_ISSUE


def test_stuck_run_recovers_when_linked_issue_goes_terminal():
	stuck = _probe(_run(25))
	terminal = _probe(_run(25), terminal=True)
	outcome, actions = _run_watchdog(_cfg(), [stuck, terminal])
	assert outcome.action == ACTION_RECOVERED
	assert outcome.attempts_made == 1


def test_stuck_run_recovers_when_fresh_healthy_run_admitted():
	stuck = _probe(_run(25))
	# A newer, younger base run appeared (still in-flight but young → healthy).
	fresh = HeartbeatRun(
		id="run-fresh",
		status="running",
		triggered_at_epoch=NOW - 120,
		completed_at_epoch=None,
		linked_issue_id="issue-fresh",
	)
	post = _probe(fresh, all_runs=(fresh,))
	outcome, _ = _run_watchdog(_cfg(), [stuck, post])
	assert outcome.action == ACTION_RECOVERED


def test_recovery_owner_is_never_the_stuck_agent_ceo():
	# The FLO-395 invariant: CEO's recovery owner is the configured peer, never CEO.
	stuck = _probe(_run(25))
	outcome, actions = _run_watchdog(_cfg(), [stuck, _probe(None)])
	assert outcome.action == ACTION_RECOVERED
	assert actions.releases[0][1] == ARCHITECT_ID
	assert actions.releases[0][1] != CEO_ID


def test_recovery_owner_for_report_is_their_manager():
	# DevOps stuck → recovery owner is the Architect (DevOps's manager).
	cfg = _cfg(watched=DEVOPS_ID, fallback=None)
	stuck = _probe(_run(25))
	outcome, actions = _run_watchdog(cfg, [stuck, _probe(None)])
	assert outcome.action == ACTION_RECOVERED
	assert actions.releases[0][1] == ARCHITECT_ID


def test_exponential_backoff_between_attempts_is_observed():
	# A reader that never recovers; assert the loop respected 30s/60s backoff.
	stuck = _probe(_run(25))
	sleeps: list[float] = []
	actions = RecordingRecoveryActions()
	outcome = run_watchdog(
		_cfg(),
		ScriptedLivenessReader([stuck]),
		actions,
		now_epoch=NOW,
		sleep=lambda s: sleeps.append(s),
	)
	assert outcome.action == ACTION_ESCALATE_BOARD
	# 3 attempts: attempt 1 immediate (admit wait only), attempt 2 +30s, attempt 3 +60s.
	# Each attempt also sleeps the admit window (45s). So sleeps = [45, 30, 45, 60, 45]
	# in order: admit(1), backoff30+admit(2)? No — backoff precedes the attempt.
	# Order per attempt: backoff (if any) then admit. So: [admit, 30, admit, 60, admit].
	assert sleeps == [45.0, 30, 45.0, 60, 45.0]


# --------------------------------------------------------------------------- #
# Escalation (Step 4): exhaustion, abort, forbidden release, unsafe owner
# --------------------------------------------------------------------------- #
def test_exhausted_attempts_escalate_to_board_with_no_fourth_attempt():
	stuck = _probe(_run(25))
	outcome, actions = _run_watchdog(_cfg(), [stuck])
	assert outcome.action == ACTION_ESCALATE_BOARD
	assert outcome.attempts_made == 3  # exactly MAX_ATTEMPTS, never 4
	# One board approval opened, linked to the watchdog + stuck issue.
	assert len(actions.approvals) == 1
	issue_ids = actions.approvals[0]["issueIds"]
	assert WATCHDOG_ISSUE in issue_ids
	assert LINKED_ISSUE in issue_ids


def test_abort_age_escalates_without_any_attempt():
	# age >= T_ABORT(45) → classify ABORT → plan ESCALATE immediately.
	outcome, actions = _run_watchdog(_cfg(), [_probe(_run(50))])
	assert outcome.action == ACTION_ESCALATE_BOARD
	assert outcome.attempts_made == 0  # never tried — straight to board
	assert actions.releases == []
	assert len(actions.approvals) == 1


def test_forbidden_release_escalates_after_one_attempt():
	stuck = _probe(_run(25))
	actions = RecordingRecoveryActions(forbid_release=True)
	outcome = run_watchdog(
		_cfg(),
		ScriptedLivenessReader([stuck, stuck]),
		actions,
		now_epoch=NOW,
		sleep=lambda _s: None,
	)
	assert outcome.action == ACTION_ESCALATE_BOARD
	assert outcome.attempts_made == 1  # the forbidden release counts as the attempt
	assert actions.invokes == []  # never got past release to invoke
	assert len(actions.approvals) == 1


def test_no_safe_recovery_owner_escalates():
	# CEO stuck but NO fallback peer configured → owner is None → escalate safely.
	cfg = _cfg(fallback=None)
	stuck = _probe(_run(25))
	outcome, actions = _run_watchdog(cfg, [stuck])
	assert outcome.action == ACTION_ESCALATE_BOARD
	assert actions.releases == []
	assert len(actions.approvals) == 1


def test_unknown_watched_agent_is_a_noop_not_a_wedge():
	# An agent id absent from the chain must surface, not silently recover.
	cfg = _cfg(watched="ghost-agent")
	outcome, actions = _run_watchdog(cfg, [_probe(_run(25))])
	assert outcome.action == ACTION_NOOP
	assert actions.releases == []


# --------------------------------------------------------------------------- #
# Suppressions: zombie (FLO-419) + missing-disposition (FLO-771)
# --------------------------------------------------------------------------- #
def test_zombie_run_superseded_by_newer_base_run_is_healthy():
	old = _run(40)
	newer = HeartbeatRun(
		id="run-newer",
		status="running",
		triggered_at_epoch=NOW - 60,
		completed_at_epoch=None,
		linked_issue_id="issue-newer",
	)
	outcome, actions = _run_watchdog(_cfg(), [_probe(old, all_runs=(old, newer))])
	assert outcome.action == ACTION_NOOP
	assert outcome.verdict.verdict == "healthy"
	assert actions.releases == []


def test_successful_run_missing_disposition_is_healthy():
	# 40m-old run, issue still active, but under a missing_disposition recovery
	# whose cause is successful_run_missing_state → the run succeeded; HEALTHY.
	outcome, actions = _run_watchdog(_cfg(), [_probe(_run(40), missing_disposition=True)])
	assert outcome.action == ACTION_NOOP
	assert outcome.verdict.verdict == "healthy"
	assert actions.releases == []


# --------------------------------------------------------------------------- #
# Dry-run + outcome shape
# --------------------------------------------------------------------------- #
def test_dry_run_never_mutates_but_reports_verdict():
	stuck = _probe(_run(25))
	outcome, actions = _run_watchdog(_cfg(), [stuck], dry_run=True)
	assert outcome.action == ACTION_DRY_RUN
	assert actions.releases == []
	assert actions.invokes == []
	assert actions.approvals == []
	assert outcome.comment_body == ""  # dry-run does not post
	# The verdict + intended action still surface in the audit.
	assert any("stuck" in line for line in outcome.audit)


def test_outcome_audit_records_each_release_and_invoke():
	stuck = _probe(_run(25))
	outcome, _ = _run_watchdog(_cfg(), [stuck])  # never recovers → 3 attempts
	audit = "\n".join(outcome.audit)
	assert "verdict=stuck" in audit
	assert audit.count("released issue") == 3
	assert audit.count("invoked heartbeat") == 3
	assert "stop retrying" in audit


def test_custom_thresholds_and_attempts_are_honoured():
	# A tighter config: 2 max attempts. Stuck run that never recovers → 2 attempts.
	cfg = _cfg(max_attempts=2)
	stuck = _probe(_run(25))
	outcome, _ = _run_watchdog(cfg, [stuck])
	assert outcome.action == ACTION_ESCALATE_BOARD
	assert outcome.attempts_made == 2


def test_protocol_conformance_of_test_doubles():
	# The fakes must satisfy the ports (runtime_checkable Protocols) so the live
	# adapters and the fakes are interchangeable.
	from tools.ops.agent_liveness.watchdog import LivenessReader, RecoveryActions

	assert isinstance(ScriptedLivenessReader([_probe(None)]), LivenessReader)
	assert isinstance(RecordingRecoveryActions(), RecoveryActions)


# --------------------------------------------------------------------------- #
# Assignment-driven detection (FLO-980) — agents with no heartbeat routine.
#
# Liveness is read from the platform's open "Review silent active run for
# {name}" issue instead of a routine's run history (runbook §Step 1). The
# decision core is unchanged; this section pins the reader adapter, the config
# gate, and that the full loop behaves identically to the heartbeat path.
# --------------------------------------------------------------------------- #
def _iso(epoch: float) -> str:
	"""Epoch -> the ``...Z`` ISO token the review-issue body carries."""
	return datetime.fromtimestamp(epoch, tz=UTC).strftime("%Y-%m-%dT%H:%M:%S.") + "000Z"


def _review_issue(
	*,
	run_id: str = "run-stuck",
	source: str = LINKED_ISSUE,
	started_epoch: float,
	status: str = "in_progress",
	agent_name: str = "DevOpsEngineer",
) -> dict:
	"""A platform stale-run review issue, in the real FLO-477 body shape."""
	return {
		"id": "review-issue-id",
		"identifier": "FLO-999",
		"title": f"Review silent active run for {agent_name}",
		"status": status,
		"originKind": "stale_active_run_evaluation",
		"originRunId": run_id,
		"parentId": source,
		"createdAt": "2026-06-20T16:00:00.000Z",
		"description": (
			"Paperclip detected critical output silence on an active run.\n\n"
			"## Run\n\n"
			f"- Run: {run_id}\n"
			f"- Agent: {agent_name}\n"
			f"- Source issue: {source}\n"
			f"- Started at: {_iso(started_epoch)}\n"
			"- Last output at: 2026-06-20T15:56:15.134Z\n"
		),
	}


class _FakeAPI:
	"""Route ``PaperclipLivenessReader`` GETs by URL to scripted JSON payloads.

	``issues_pages`` is a queue (one search-result page per ``probe()``) so a
	recovery loop can model "open issue, then cleared". Chain + linked-issue
	responses are constant across probes.
	"""

	def __init__(self, *, agents, issues_pages, linked_status="in_progress", linked_action=None):
		self.agents = agents
		self.issues_pages = list(issues_pages)
		self.linked_status = linked_status
		self.linked_action = linked_action
		self._i = 0

	def __call__(self, url, headers):
		if "/agents" in url:
			return json.dumps(self.agents)
		if "/issues?" in url:  # the stale-run-issue search
			if self._i < len(self.issues_pages):
				page = self.issues_pages[self._i]
				self._i += 1
			else:
				page = self.issues_pages[-1] if self.issues_pages else []
			return json.dumps(page)
		# /api/issues/{id} — linked source-issue status lookup.
		payload = {"status": self.linked_status}
		if self.linked_action is not None:
			payload["activeRecoveryAction"] = self.linked_action
		return json.dumps(payload)


_AGENTS = [
	{"id": CEO_ID, "name": "CEO", "reportsTo": None},
	{"id": ARCHITECT_ID, "name": "SoftwareArchitect", "reportsTo": CEO_ID},
	{"id": DEVOPS_ID, "name": "DevOpsEngineer", "reportsTo": ARCHITECT_ID},
]


def _ad_reader(issues_pages, *, linked_status="in_progress", linked_action=None):
	"""A live reader over a fake API, wired for the assignment-driven path."""
	return PaperclipLivenessReader(
		api_url="https://example.test",
		api_key="token",
		company_id="co",
		watched_agent_id=DEVOPS_ID,
		watched_agent_name="DevOpsEngineer",
		http_get=_FakeAPI(
			agents=_AGENTS,
			issues_pages=issues_pages,
			linked_status=linked_status,
			linked_action=linked_action,
		),
	)


def _ad_cfg(**kw) -> WatchdogConfig:
	"""Assignment-driven config: agent name set, NO heartbeat routine."""
	return _cfg(
		watched=DEVOPS_ID,
		fallback=None,
		watched_routine_id=None,
		watched_agent_name="DevOpsEngineer",
		**kw,
	)


def _run_watchdog_with_reader(config, reader, actions=None, **kw):
	"""Drive ``run_watchdog`` with a real reader + recording sink (no sleep)."""
	actions = actions or RecordingRecoveryActions()
	outcome = run_watchdog(config, reader, actions, now_epoch=NOW, sleep=lambda _s: None, **kw)
	return outcome, actions


def test_parse_started_at_extracts_timestamp_from_review_body():
	from tools.ops.agent_liveness.watchdog import _parse_started_at

	body = _review_issue(started_epoch=NOW - 25 * 60)["description"]
	assert _parse_started_at(body) == _iso(NOW - 25 * 60)
	assert _parse_started_at("no marker here") is None
	assert _parse_started_at(None) is None


def test_reader_requires_a_detection_input():
	# Neither routine id nor agent name -> misconfiguration, surfaced not wedged.
	import pytest

	with pytest.raises(ValueError):
		PaperclipLivenessReader(api_url="x", api_key="y", company_id="c", watched_agent_id=DEVOPS_ID)


def test_assignment_driven_reader_returns_none_when_no_open_review_issue():
	# A resolved (done) review issue must NOT count as a stuck signal.
	reader = _ad_reader([[_review_issue(started_epoch=NOW - 25 * 60, status="done")]])
	assert reader.probe().run is None  # healthy — no open issue


def test_assignment_driven_reader_builds_run_from_open_review_issue():
	reader = _ad_reader([[_review_issue(started_epoch=NOW - 25 * 60, run_id="run-9")]])
	probe = reader.probe()
	assert probe.run is not None
	assert probe.run.id == "run-9"
	# linked issue = the stuck run's SOURCE issue (parentId) — the Step 3 target.
	assert probe.run.linked_issue_id == LINKED_ISSUE
	# triggeredAt parsed from the body's "Started at" line.
	assert probe.run.triggered_at_epoch == NOW - 25 * 60


def test_assignment_driven_no_open_issue_is_healthy_noop():
	outcome, actions = _run_watchdog_with_reader(_ad_cfg(), _ad_reader([[]]))
	assert outcome.action == ACTION_NOOP
	assert actions.releases == []
	assert actions.invokes == []


def test_assignment_driven_stuck_run_recovers_and_releases_source_issue():
	cfg = _ad_cfg()
	# Page 1: open stuck review issue (age 25m >= T_STUCK); page 2: cleared.
	reader = _ad_reader(
		[
			[_review_issue(started_epoch=NOW - 25 * 60)],
			[],
		]
	)
	outcome, actions = _run_watchdog_with_reader(cfg, reader)
	assert outcome.action == ACTION_RECOVERED
	assert outcome.attempts_made == 1
	# Recovery owner is DevOps's manager (Architect), never DevOps (FLO-395).
	assert actions.releases[0] == (LINKED_ISSUE, ARCHITECT_ID)
	assert actions.invokes == [DEVOPS_ID]


def test_assignment_driven_stuck_run_escalates_when_never_recovers():
	cfg = _ad_cfg()
	# The review issue stays open across every re-detect -> 3 attempts -> board.
	reader = _ad_reader([[_review_issue(started_epoch=NOW - 25 * 60)]])
	outcome, actions = _run_watchdog_with_reader(cfg, reader)
	assert outcome.action == ACTION_ESCALATE_BOARD
	assert outcome.attempts_made == 3
	assert len(actions.approvals) == 1


def test_assignment_driven_missing_disposition_is_healthy():
	# 40m-old open review issue, but the source run SUCCEEDED (missing
	# disposition) -> HEALTHY, no release+restart (FLO-771 suppression).
	cfg = _ad_cfg()
	reader = _ad_reader(
		[[_review_issue(started_epoch=NOW - 40 * 60)]],
		linked_action={"kind": "missing_disposition", "cause": "successful_run_missing_state"},
	)
	outcome, actions = _run_watchdog_with_reader(cfg, reader)
	assert outcome.action == ACTION_NOOP
	assert outcome.verdict.verdict == "healthy"
	assert actions.releases == []


def test_assignment_driven_terminal_source_issue_is_healthy():
	# Open review issue but the source issue is already done -> healthy.
	cfg = _ad_cfg()
	reader = _ad_reader(
		[[_review_issue(started_epoch=NOW - 40 * 60)]],
		linked_status="done",
	)
	outcome, actions = _run_watchdog_with_reader(cfg, reader)
	assert outcome.action == ACTION_NOOP
	assert actions.releases == []
