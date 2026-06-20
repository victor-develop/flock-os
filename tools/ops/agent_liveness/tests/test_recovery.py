"""
Agent-liveness recovery lock (FLO-395).

Pins the generalization of the CEO liveness pattern
([FLO-267](/FLO/issues/FLO-267)) to the whole chain of command. The
canonical negative test — a simulated silent Software Architect run
self-heals — directly reproduces the 2026-06-20 [FLO-365](/FLO/issues/FLO-365)
incident shape and asserts the core FLO-395 fix: the recovery owner resolves
to the **CEO** (the Architect's manager), never to the stuck Architect, and
the release+restart loop escalates to the board once attempts are exhausted.

Pure logic: no bench, no Frappe, no network. Builds the chain of command +
hand-built :class:`HeartbeatRun` records and asserts the verdict,
recovery-owner, and recovery-plan math against the runbook's thresholds.

The live Paperclip REST calls (release / heartbeat invoke / board approval)
are the watchdog routine procedure documented in
`docs/operations/agent-liveness-recovery-runbook.md`; this suite pins the
deterministic core that procedure obeys.
"""

from __future__ import annotations

import pytest

from tools.ops.agent_liveness.recovery import (
	ACTION_ESCALATE_BOARD,
	ACTION_NOOP,
	ACTION_RELEASE_RESTART,
	DEFAULT_BACKOFF_SECONDS,
	DEFAULT_MAX_ATTEMPTS,
	DEFAULT_T_ABORT_MINUTES,
	DEFAULT_T_ESCALATE_MINUTES,
	AgentNode,
	ChainOfCommand,
	LivenessThresholds,
	RunVerdict,
	classify_run,
	plan_recovery,
	recovery_owner_table,
	resolve_recovery_owner,
)
from tools.ops.ceo_heartbeat.monitor import HeartbeatRun

# ---------------------------------------------------------------------------- #
# Fixtures — the Flock OS chain of command + a simulated silent run.
# ---------------------------------------------------------------------------- #
# Agent ids are the real Paperclip ids for this company (resolved during
# FLO-395). CEO is the top of the chain; Architect reports to CEO; the four
# delivery agents report to the Architect. This is the exact topology that
# made the FLO-365 incident unrecoverable: the Architect is the #2 agent and
# its manager (CEO) is the only valid recovery owner.
CEO_ID = "d572935c-f075-471f-aab8-0bd2d9a975ba"
ARCHITECT_ID = "f893dff0-4d7c-4d24-9234-a8d5de384fa1"
DEVOPS_ID = "e35458ca-b563-4394-a9d7-e73c28fd6b77"
FRONTEND_ID = "a95965d2-ded9-4ffa-84db-0aa7f7b31fe7"
QA_ID = "ca835526-10f3-4e9e-a08e-1e7c74db636c"
BACKEND_ID = "8eee3659-ca1c-42ea-9aa7-485818f56536"
# Architect is the configured peer that owns the CEO watchdog routine; when
# the CEO itself is stuck, recovery routes here (the CEO has no manager).
ARCHITECT_AS_CEO_FALLBACK = ARCHITECT_ID

CHAIN = ChainOfCommand.from_agents(
	[
		AgentNode(id=CEO_ID, name="CEO", reports_to=None),
		AgentNode(id=ARCHITECT_ID, name="SoftwareArchitect", reports_to=CEO_ID),
		AgentNode(id=DEVOPS_ID, name="DevOpsEngineer", reports_to=ARCHITECT_ID),
		AgentNode(id=FRONTEND_ID, name="FrontendEngineer", reports_to=ARCHITECT_ID),
		AgentNode(id=QA_ID, name="QAEngineer", reports_to=ARCHITECT_ID),
		AgentNode(id=BACKEND_ID, name="BackendEngineer", reports_to=ARCHITECT_ID),
	]
)

# The simulated FLO-365 silent Architect run. ``triggeredAt`` is 25 minutes
# before ``now`` → age 25m, past T_STUCK (20) but below T_ABORT (45). The
# linked issue (FLO-365) is still in_progress, so this is a STUCK verdict.
NOW_EPOCH = 1_800_000_000.0  # arbitrary fixed clock for deterministic math
SILENT_ARCHITECT_RUN = HeartbeatRun(
	id="c8bfd1cb-e70e-41e5-87bd-7011372cac34",  # the real FLO-365 run id
	status="issue_created",  # active, non-terminal
	triggered_at_epoch=NOW_EPOCH - (25 * 60),
	completed_at_epoch=None,
	linked_issue_id="90a4d819-0000-0000-0000-000000000365",
	linked_issue_identifier="FLO-365",
	linked_issue_title="Phase 6.1 15k data-tier stress",
	linked_issue_status="in_progress",
	coalesced_into_run_id=None,
	failure_reason=None,
)
THRESHOLDS = LivenessThresholds()


# ---------------------------------------------------------------------------- #
# resolve_recovery_owner — the core FLO-395 fix.
# ---------------------------------------------------------------------------- #
@pytest.mark.parametrize(
	("stuck", "expected_owner"),
	[
		(ARCHITECT_ID, CEO_ID),  # Architect -> CEO  (the FLO-365 incident path)
		(DEVOPS_ID, ARCHITECT_ID),  # DevOps -> Architect
		(FRONTEND_ID, ARCHITECT_ID),  # Frontend -> Architect
		(QA_ID, ARCHITECT_ID),  # QA -> Architect
		(BACKEND_ID, ARCHITECT_ID),  # Backend -> Architect
		(CEO_ID, ARCHITECT_AS_CEO_FALLBACK),  # CEO -> configured peer (FLO-267)
	],
)
def test_recovery_owner_is_the_manager_never_the_stuck_agent(stuck, expected_owner):
	"""The whole point of FLO-395: recovery owner is the manager, never self."""
	owner = resolve_recovery_owner(stuck, CHAIN, ceo_fallback_owner_id=ARCHITECT_AS_CEO_FALLBACK)
	assert owner == expected_owner
	assert owner != stuck, "recovery must never route back to the stuck agent"


def test_ceo_recovery_returns_none_when_no_fallback_configured():
	"""A misconfigured CEO watchdog (no peer fallback) surfaces as None.

	The watchdog must surface this as a config error, not silently wedge — so
	resolve_recovery_owner returns None rather than falling back to the CEO.
	"""
	owner = resolve_recovery_owner(CEO_ID, CHAIN, ceo_fallback_owner_id=None)
	assert owner is None


def test_unknown_agent_recovers_to_none_not_self():
	"""An agent not in the chain yields None (treated as top-of-chain); never itself."""
	owner = resolve_recovery_owner("unknown-agent-id", CHAIN, ceo_fallback_owner_id=ARCHITECT_AS_CEO_FALLBACK)
	# No manager + CEO fallback applies only to the CEO id; an unknown id has
	# no fallback and must not resolve to itself.
	assert owner is None


# ---------------------------------------------------------------------------- #
# classify_run — the HEALTHY / SUSPICIOUS / STUCK / ABORT verdict table.
# ---------------------------------------------------------------------------- #
def _run(
	age_minutes: float,
	*,
	status: str = "issue_created",
	issue_status: str = "in_progress",
) -> HeartbeatRun:
	return HeartbeatRun(
		id=f"run-age-{age_minutes:.0f}",
		status=status,
		triggered_at_epoch=NOW_EPOCH - (age_minutes * 60),
		completed_at_epoch=None,
		linked_issue_id="linked-issue-id",
		linked_issue_identifier="FLO-X",
		linked_issue_title="simulated",
		linked_issue_status=issue_status,
		coalesced_into_run_id=None,
		failure_reason=None,
	)


def test_classify_healthy_within_normal_bounds():
	run = _run(age_minutes=5)
	verdict = classify_run(
		run,
		now_epoch=NOW_EPOCH,
		stuck_agent_id=ARCHITECT_ID,
		chain=CHAIN,
		thresholds=THRESHOLDS,
	)
	assert verdict.verdict == "healthy"
	assert verdict.recovery_owner_id == CEO_ID


def test_classify_suspicious_in_watch_window():
	run = _run(age_minutes=17)  # >= T_SUSPICIOUS (15), < T_STUCK (20)
	verdict = classify_run(
		run,
		now_epoch=NOW_EPOCH,
		stuck_agent_id=ARCHITECT_ID,
		chain=CHAIN,
		thresholds=THRESHOLDS,
	)
	assert verdict.verdict == "suspicious"
	assert verdict.recovery_owner_id == CEO_ID


def test_classify_stuck_when_age_past_threshold_and_issue_active():
	"""The FLO-365 shape: 25m old, linked issue still in_progress."""
	verdict = classify_run(
		SILENT_ARCHITECT_RUN,
		now_epoch=NOW_EPOCH,
		stuck_agent_id=ARCHITECT_ID,
		chain=CHAIN,
		thresholds=THRESHOLDS,
	)
	assert verdict.verdict == "stuck"
	assert verdict.age_minutes == pytest.approx(25.0, abs=0.01)
	assert verdict.stuck_agent_id == ARCHITECT_ID
	# The core fix — owner is the CEO, NOT the stuck Architect.
	assert verdict.recovery_owner_id == CEO_ID
	assert verdict.recovery_owner_id != ARCHITECT_ID


def test_classify_healthy_when_linked_issue_terminal():
	"""A run whose linked issue is done/cancelled is not stuck, regardless of age."""
	verdict = classify_run(
		SILENT_ARCHITECT_RUN,
		now_epoch=NOW_EPOCH,
		stuck_agent_id=ARCHITECT_ID,
		chain=CHAIN,
		thresholds=THRESHOLDS,
		linked_issue_is_terminal=True,
	)
	assert verdict.verdict == "healthy"


def test_classify_abort_past_t_abort():
	run = _run(age_minutes=DEFAULT_T_ABORT_MINUTES + 5)  # 50m
	verdict = classify_run(
		run,
		now_epoch=NOW_EPOCH,
		stuck_agent_id=ARCHITECT_ID,
		chain=CHAIN,
		thresholds=THRESHOLDS,
	)
	assert verdict.verdict == "abort"


def test_classify_devops_run_recovers_to_architect():
	"""A silent DevOps run recovers to the Architect (DevOps's manager)."""
	run = _run(age_minutes=25)
	verdict = classify_run(
		run,
		now_epoch=NOW_EPOCH,
		stuck_agent_id=DEVOPS_ID,
		chain=CHAIN,
		thresholds=THRESHOLDS,
	)
	assert verdict.verdict == "stuck"
	assert verdict.recovery_owner_id == ARCHITECT_ID


# ---------------------------------------------------------------------------- #
# plan_recovery — NOOP / RELEASE_RESTART / ESCALATE_BOARD.
# ---------------------------------------------------------------------------- #
def _stuck_verdict(age: float = 25.0, owner: str = CEO_ID) -> RunVerdict:
	return RunVerdict(
		verdict="stuck",
		age_minutes=age,
		stuck_agent_id=ARCHITECT_ID,
		recovery_owner_id=owner,
		linked_issue_id="linked",
		reason="stuck",
	)


def test_plan_recovery_healthy_is_noop():
	verdict = RunVerdict(
		verdict="healthy",
		age_minutes=5,
		stuck_agent_id=ARCHITECT_ID,
		recovery_owner_id=CEO_ID,
		linked_issue_id="linked",
		reason="ok",
	)
	plan = plan_recovery(verdict, prior_attempts=0)
	assert plan.action == ACTION_NOOP
	assert plan.next_backoff_seconds is None


def test_plan_recovery_stuck_first_attempt_releases_immediately():
	plan = plan_recovery(_stuck_verdict(), prior_attempts=0)
	assert plan.action == ACTION_RELEASE_RESTART
	assert plan.next_backoff_seconds == 0  # first attempt is immediate
	assert plan.attempts_remaining == DEFAULT_MAX_ATTEMPTS - 1
	assert plan.recovery_owner_id == CEO_ID


def test_plan_recovery_backoff_schedule_advances():
	"""Attempt 2 sleeps 30s, attempt 3 sleeps 60s (matches the runbook)."""
	plan2 = plan_recovery(_stuck_verdict(), prior_attempts=1)
	assert plan2.action == ACTION_RELEASE_RESTART
	assert plan2.next_backoff_seconds == DEFAULT_BACKOFF_SECONDS[0]  # 30

	plan3 = plan_recovery(_stuck_verdict(), prior_attempts=2)
	assert plan3.action == ACTION_RELEASE_RESTART
	assert plan3.next_backoff_seconds == DEFAULT_BACKOFF_SECONDS[1]  # 60


def test_plan_recovery_escalates_when_attempts_exhausted():
	"""After MAX_ATTEMPTS failed release+restarts, escalate to the board."""
	plan = plan_recovery(_stuck_verdict(), prior_attempts=DEFAULT_MAX_ATTEMPTS)
	assert plan.action == ACTION_ESCALATE_BOARD
	assert plan.attempts_remaining == 0
	assert plan.next_backoff_seconds is None
	assert plan.recovery_owner_id == CEO_ID


def test_plan_recovery_escalates_past_t_escalate_even_with_attempts_left():
	"""age >= T_ESCALATE (30m) short-circuits to escalation regardless of attempts."""
	plan = plan_recovery(
		_stuck_verdict(age=DEFAULT_T_ESCALATE_MINUTES + 5),
		prior_attempts=1,
	)
	assert plan.action == ACTION_ESCALATE_BOARD


def test_plan_recovery_abort_verdict_escalates():
	verdict = RunVerdict(
		verdict="abort",
		age_minutes=50,
		stuck_agent_id=ARCHITECT_ID,
		recovery_owner_id=CEO_ID,
		linked_issue_id="linked",
		reason="abort",
	)
	plan = plan_recovery(verdict, prior_attempts=0)
	assert plan.action == ACTION_ESCALATE_BOARD


# ---------------------------------------------------------------------------- #
# The canonical negative test — a simulated silent Architect run self-heals.
# ---------------------------------------------------------------------------- #
def test_simulated_silent_architect_run_self_heals():
	"""End-to-end (logic-only) replay of the FLO-365 incident.

	A silent Architect run is detected, the recovery owner resolves to the
	CEO (never the Architect), release+restart is attempted with backoff up
	to MAX_ATTEMPTS, and on persistent failure the watchdog escalates to the
	board. This is the exact path that was missing on 2026-06-20 and that
	FLO-395 closes.
	"""
	# 1. Detect: classify the silent run.
	verdict = classify_run(
		SILENT_ARCHITECT_RUN,
		now_epoch=NOW_EPOCH,
		stuck_agent_id=ARCHITECT_ID,
		chain=CHAIN,
		thresholds=THRESHOLDS,
	)
	assert verdict.verdict == "stuck"

	# 2. Resolve recovery owner — the core fix. Must be the CEO, not self.
	assert verdict.recovery_owner_id == CEO_ID
	assert verdict.recovery_owner_id != ARCHITECT_ID

	# 3. Attempt release+restart with backoff, up to MAX_ATTEMPTS.
	actions: list[str] = []
	for attempt in range(DEFAULT_MAX_ATTEMPTS):
		plan = plan_recovery(verdict, prior_attempts=attempt)
		actions.append(plan.action)
		assert plan.recovery_owner_id == CEO_ID  # owner never drifts to self
		if plan.action == ACTION_RELEASE_RESTART:
			# Simulate the restart failing to admit (the run stays silent).
			continue
		break
	# All MAX_ATTEMPTS attempts were release+restart (not a premature escalate).
	assert actions[:DEFAULT_MAX_ATTEMPTS] == [ACTION_RELEASE_RESTART] * DEFAULT_MAX_ATTEMPTS

	# 4. After the budget is exhausted, the next plan escalates to the board.
	escalate = plan_recovery(verdict, prior_attempts=DEFAULT_MAX_ATTEMPTS)
	assert escalate.action == ACTION_ESCALATE_BOARD
	assert escalate.recovery_owner_id == CEO_ID

	# The watchdog would now POST a board approval (the runbook's Step 4);
	# the logic has done its job — no human needed for detection, owner
	# resolution, or the retry loop. The board is only paged when automation
	# is genuinely exhausted, which is the FLO-267 contract preserved.


# ---------------------------------------------------------------------------- #
# Recovery-owner table — the runbook's §Coverage is generated from this.
# ---------------------------------------------------------------------------- #
def test_recovery_owner_table_covers_all_agents_with_managers():
	"""The runbook coverage table must list every agent and its recovery owner."""
	table = recovery_owner_table(CHAIN, ceo_fallback_owner_id=ARCHITECT_AS_CEO_FALLBACK)
	names = {row[0] for row in table}
	assert names == {
		"CEO",
		"SoftwareArchitect",
		"DevOpsEngineer",
		"FrontendEngineer",
		"QAEngineer",
		"BackendEngineer",
	}
	by_name = {row[0]: row for row in table}
	# The FLO-365 incident row — Architect's recovery owner is the CEO.
	assert by_name["SoftwareArchitect"][2] == "CEO"
	# DevOps → Architect.
	assert by_name["DevOpsEngineer"][2] == "SoftwareArchitect"
	# CEO → configured peer (Architect).
	assert by_name["CEO"][2] == "SoftwareArchitect"
