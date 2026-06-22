#!/usr/bin/env python3
"""
Agent-liveness recovery watchdog (FLO-968).

WHAT
----
The deterministic runner for the agent-liveness recovery runbook
(``docs/operations/agent-liveness-recovery-runbook.md``). On each fire it
detects whether a watched agent's latest heartbeat run is stuck/silent, and
when it is, recovers it automatically: release the stuck checkout slot, invoke
a fresh heartbeat run, with exponential backoff (30s→60s→120s) up to 3
attempts. When automation is exhausted (or a release is forbidden), it opens a
board approval so a human can authorise force-release / pause-resume.

WHY A SCRIPT (not hand-executed curl)
-------------------------------------
The runbook always documented the procedure, and the decision logic lives in
``tools/ops/agent_liveness/recovery.py``. But the recovery-owner agent used to
hand-execute the REST steps in its heartbeat — which is fragile. The
[FLO-968](/FLO/issues/FLO-968) CEO watchdog fire failed mid-run with a
transient adapter error, leaving the liveness safeguard stranded with no live
path. This script is the single entry point the recovery-owner invokes instead,
so the detect → release → restart → escalate loop is deterministic and the
outcome is always recorded as a runbook comment.

DESIGN
------
* The orchestration (ports + adapters + the Step 1–4 loop) lives in
  ``tools.ops.agent_liveness.watchdog`` and is pure given injected ports — so
  it is unit-tested by ``tools/ops/agent_liveness/tests/test_watchdog.py`` and
  exercised by ``--self-test``. The CLI only adds env wiring + stdout.
* Stdlib only (``urllib``) — no third-party deps, runs in the bare CI venv and
  on the operator Mac without ``pip install``.
* Idempotent + safe to cron: re-evaluates from the API each fire; a healthy
  agent is a no-op comment; a forbidden release escalates instead of looping.

USAGE
-----
    # One recovery pass (the routine-fire entry point):
    python3 scripts/dev/agent-liveness-watchdog.py

    # Observe-only: detect + report, release/invoke/escalate nothing
    # (runbook Verification #1 — fire while paused, expect healthy + no action):
    python3 scripts/dev/agent-liveness-watchdog.py --dry-run

    # Pure-logic self-test (no API, no mutations) — the runbook gate:
    python3 scripts/dev/agent-liveness-watchdog.py --self-test

Env (required for live recovery, ignored by ``--self-test``):

    PAPERCLIP_API_URL     base API URL
    PAPERCLIP_API_KEY     bearer token (run JWT or long-lived agent key)
    PAPERCLIP_COMPANY_ID  company id hosting the watched agent + routine
    PAPERCLIP_RUN_ID      current run id (attached to mutations for traceability)
    PAPERCLIP_TASK_ID     the watchdog run issue (comment target)

Optional:

    FLO_LIVENESS_WATCHED_AGENT_ID    watched agent id (else error)
    FLO_LIVENESS_WATCHED_ROUTINE_ID  watched heartbeat routine id (else error)
    FLO_LIVENESS_CEO_FALLBACK_OWNER  configured peer for the CEO (Architect id)
    FLO_LIVENESS_WATCHDOG_ISSUE_ID   override the comment target issue id
    FLO_LIVENESS_PROJECT_ID          tag board approvals with this project
    FLO_LIVENESS_GOAL_ID             tag board approvals with this goal

See ``docs/operations/agent-liveness-recovery-runbook.md`` for the full
procedure, constants, and coverage table.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# scripts/dev/ -> repo root must be importable for the package import below
# (the orchestration core lives in tools/ops/agent_liveness/watchdog.py). CI
# installs the app package, but a direct ``python3 scripts/dev/...`` invocation
# needs the repo root on sys.path, mirroring how the test suite resolves it.
_REPO_ROOT = str(Path(__file__).resolve().parents[2])
if _REPO_ROOT not in sys.path:
	sys.path.insert(0, _REPO_ROOT)

# Operator tooling: import the orchestration core as a package (the flock_os
# app package is installed in CI; locally the repo root is on sys.path).
from tools.ops.agent_liveness.watchdog import (  # noqa: E402
	ACTION_DRY_RUN,
	ACTION_ESCALATE_BOARD,
	ACTION_NOOP,
	ACTION_RECOVERED,
	PaperclipLivenessReader,
	PaperclipRecoveryActions,
	RecordingRecoveryActions,
	WatchdogConfig,
	run_watchdog,
)


def build_config() -> WatchdogConfig:
	"""Resolve :class:`WatchdogConfig` from the environment."""
	watched_agent = os.environ.get("FLO_LIVENESS_WATCHED_AGENT_ID")
	watched_routine = os.environ.get("FLO_LIVENESS_WATCHED_ROUTINE_ID")
	missing = [
		name
		for name, val in (
			("FLO_LIVENESS_WATCHED_AGENT_ID", watched_agent),
			("FLO_LIVENESS_WATCHED_ROUTINE_ID", watched_routine),
		)
		if not val
	]
	if missing:
		raise SystemExit(
			"error: missing required env: " + ", ".join(missing) + "\n"
			"  (set the watched agent + routine ids, or use --self-test / --dry-run for the logic check.)"
		)
	return WatchdogConfig(
		watched_agent_id=watched_agent,  # type: ignore[arg-type]
		watched_routine_id=watched_routine,  # type: ignore[arg-type]
		ceo_fallback_owner_id=os.environ.get("FLO_LIVENESS_CEO_FALLBACK_OWNER"),
		watchdog_issue_id=os.environ.get(
			"FLO_LIVENESS_WATCHDOG_ISSUE_ID", os.environ.get("PAPERCLIP_TASK_ID")
		),
		project_id=os.environ.get("FLO_LIVENESS_PROJECT_ID"),
		goal_id=os.environ.get("FLO_LIVENESS_GOAL_ID"),
	)


def _exit_code(action: str) -> int:
	"""Map the outcome action to a process exit code (monitoring convention)."""
	if action == ACTION_ESCALATE_BOARD:
		return 2  # critical — needed human/board attention
	if action == ACTION_RECOVERED:
		return 1  # warning — recovered, but a stuck run was observed
	return 0  # ok — healthy / noop / dry-run


def run_once(*, dry_run: bool) -> int:
	"""One watchdog pass against the live Paperclip API (or a dry-run sink)."""
	config = build_config()

	if dry_run:
		# Detect + report only: a recording sink that echoes instead of mutating.
		reader = PaperclipLivenessReader(
			api_url=os.environ["PAPERCLIP_API_URL"],
			api_key=os.environ["PAPERCLIP_API_KEY"],
			company_id=os.environ["PAPERCLIP_COMPANY_ID"],
			watched_agent_id=config.watched_agent_id,
			watched_routine_id=config.watched_routine_id,
		)
		actions = RecordingRecoveryActions(echo=lambda line: print(line))
		outcome = run_watchdog(config, reader, actions, dry_run=True)
	else:
		reader = PaperclipLivenessReader(
			api_url=os.environ["PAPERCLIP_API_URL"],
			api_key=os.environ["PAPERCLIP_API_KEY"],
			company_id=os.environ["PAPERCLIP_COMPANY_ID"],
			watched_agent_id=config.watched_agent_id,
			watched_routine_id=config.watched_routine_id,
		)
		actions = PaperclipRecoveryActions(
			api_url=os.environ["PAPERCLIP_API_URL"],
			api_key=os.environ["PAPERCLIP_API_KEY"],
			company_id=os.environ["PAPERCLIP_COMPANY_ID"],
			run_id=os.environ.get("PAPERCLIP_RUN_ID"),
		)
		outcome = run_watchdog(config, reader, actions, dry_run=False)

	for line in outcome.audit:
		print(line)
	if outcome.comment_body:
		print("---")
		print(outcome.comment_body)
	return _exit_code(outcome.action)


# =========================================================================== #
# Self-test (pure logic; no API, no mutations) — the runbook activation gate.
# =========================================================================== #
def self_test() -> int:
	"""Exercise the orchestration loop against scripted probes + a recording sink.

	Mirrors the runbook's canonical scenarios: healthy no-op, suspicious watch,
	stuck→recovered, stuck→exhausted→escalate, forbidden release→escalate, the
	zombie (FLO-419) + missing-disposition (FLO-771) suppressions, and the
	recovery-owner ≠ stuck-agent invariant. Exit 0 on pass.
	"""
	from tools.ops.agent_liveness.recovery import (
		AgentNode,
		ChainOfCommand,
	)
	from tools.ops.agent_liveness.watchdog import (
		ACTION_RELEASE_RESTART,
		LivenessProbe,
	)
	from tools.ops.ceo_heartbeat.monitor import HeartbeatRun

	cases: list[tuple[str, object, object]] = []

	ceo = "ceo-id"
	arch = "arch-id"
	chain = ChainOfCommand.from_agents(
		[
			AgentNode(id=ceo, name="CEO", reports_to=None),
			AgentNode(id=arch, name="Architect", reports_to=ceo),
		]
	)
	cfg = WatchdogConfig(
		watched_agent_id=ceo,
		watched_routine_id="routine-1",
		ceo_fallback_owner_id=arch,
		watchdog_issue_id="watchdog-issue",
	)
	now = 1_800_000_000.0

	def _run(age_no, status="issue_created"):
		return HeartbeatRun(
			id="run-1",
			status=status,
			triggered_at_epoch=now - age_no,
			completed_at_epoch=None,
			linked_issue_id="issue-1",
		)

	# healthy — no in-flight run.
	probe_none = LivenessProbe(
		run=None,
		all_runs=(),
		chain=chain,
		linked_issue_is_terminal=False,
		successful_run_missing_disposition=False,
	)
	out = run_watchdog(cfg, _ScriptedReader([probe_none]), _Sink(), now_epoch=now, sleep=_noop)
	cases.append(("healthy no-run → noop", out.action, ACTION_NOOP))

	# healthy — run within normal bounds (age 5m).
	probe_young = LivenessProbe(
		run=_run(5 * 60),
		all_runs=(_run(5 * 60),),
		chain=chain,
		linked_issue_is_terminal=False,
		successful_run_missing_disposition=False,
	)
	out = run_watchdog(cfg, _ScriptedReader([probe_young]), _Sink(), now_epoch=now, sleep=_noop)
	cases.append(("healthy young run → noop", out.action, ACTION_NOOP))

	# suspicious — age 16m (between T_SUSPICIOUS and T_STUCK): watch comment only.
	probe_susp = LivenessProbe(
		run=_run(16 * 60),
		all_runs=(_run(16 * 60),),
		chain=chain,
		linked_issue_is_terminal=False,
		successful_run_missing_disposition=False,
	)
	sink = _Sink()
	out = run_watchdog(cfg, _ScriptedReader([probe_susp]), sink, now_epoch=now, sleep=_noop)
	cases.append(("suspicious → noop", out.action, ACTION_NOOP))
	cases.append(("suspicious posts a watch comment", len(sink.comments), 1))
	cases.append(("suspicious does not release", len(sink.releases), 0))

	# stuck → recovered on attempt 1 (2nd probe is healthy).
	probe_stuck = LivenessProbe(
		run=_run(25 * 60),
		all_runs=(_run(25 * 60),),
		chain=chain,
		linked_issue_is_terminal=False,
		successful_run_missing_disposition=False,
	)
	sink = _Sink()
	out = run_watchdog(cfg, _ScriptedReader([probe_stuck, probe_none]), sink, now_epoch=now, sleep=_noop)
	cases.append(("stuck → recovered", out.action, ACTION_RECOVERED))
	cases.append(("recovery released the linked issue", len(sink.releases), 1))
	cases.append(("recovery invoked heartbeat", len(sink.invokes), 1))
	cases.append(("recovery owner is the architect", sink.releases[0][1], arch))

	# stuck → exhausted 3 attempts → escalate.
	sink = _Sink()
	out = run_watchdog(cfg, _ScriptedReader([probe_stuck]), sink, now_epoch=now, sleep=_noop)
	cases.append(("stuck never recovers → escalate", out.action, ACTION_ESCALATE_BOARD))
	cases.append(("exhausted made 3 attempts", out.attempts_made, 3))
	cases.append(("escalation opened a board approval", len(sink.approvals), 1))

	# forbidden release (403) → escalate immediately, no further attempts.
	sink = _Sink(forbid_release=True)
	out = run_watchdog(cfg, _ScriptedReader([probe_stuck, probe_stuck]), sink, now_epoch=now, sleep=_noop)
	cases.append(("forbidden release → escalate", out.action, ACTION_ESCALATE_BOARD))
	cases.append(("forbidden → exactly 1 attempt", out.attempts_made, 1))
	cases.append(("forbidden → board approval opened", len(sink.approvals), 1))

	# zombie (FLO-419): superseded by a newer base run → healthy, no release.
	newer = HeartbeatRun(
		id="run-newer",
		status="running",
		triggered_at_epoch=now - 60,
		completed_at_epoch=None,
		linked_issue_id="issue-newer",
	)
	old = _run(40 * 60)
	probe_zombie = LivenessProbe(
		run=old,
		all_runs=(old, newer),
		chain=chain,
		linked_issue_is_terminal=False,
		successful_run_missing_disposition=False,
	)
	sink = _Sink()
	out = run_watchdog(cfg, _ScriptedReader([probe_zombie]), sink, now_epoch=now, sleep=_noop)
	cases.append(("zombie run → healthy noop", out.action, ACTION_NOOP))
	cases.append(("zombie → no release", len(sink.releases), 0))

	# missing-disposition (FLO-771): run succeeded, issue lacks terminal status → healthy.
	probe_md = LivenessProbe(
		run=_run(40 * 60),
		all_runs=(_run(40 * 60),),
		chain=chain,
		linked_issue_is_terminal=False,
		successful_run_missing_disposition=True,
	)
	sink = _Sink()
	out = run_watchdog(cfg, _ScriptedReader([probe_md]), sink, now_epoch=now, sleep=_noop)
	cases.append(("missing-disposition → healthy noop", out.action, ACTION_NOOP))

	# dry-run never mutates.
	sink = _Sink()
	out = run_watchdog(cfg, _ScriptedReader([probe_stuck]), sink, now_epoch=now, sleep=_noop, dry_run=True)
	cases.append(("dry-run action label", out.action, ACTION_DRY_RUN))
	cases.append(("dry-run mutates nothing", (len(sink.releases), len(sink.invokes)), (0, 0)))

	# release_restart action label surfaces when recovered path is mid-flight —
	# sanity that the decision-core action constant is re-exported consistently.
	cases.append(("release_restart constant", ACTION_RELEASE_RESTART, "release_restart"))

	failures = 0
	for name, got, want in cases:
		ok = got == want
		if isinstance(want, bool) != isinstance(got, bool):
			ok = False
		if ok:
			print(f"  PASS  {name}")
		else:
			failures += 1
			print(f"  FAIL  {name}: got {got!r}, want {want!r}")
	print(f"self-test: {len(cases) - failures}/{len(cases)} passed")
	return 1 if failures else 0


class _ScriptedReader:
	"""Minimal scripted reader for the self-test (mirrors ScriptedLivenessReader)."""

	def __init__(self, probes):
		self._probes = list(probes)
		self._i = 0

	def probe(self):
		if self._i < len(self._probes):
			p = self._probes[self._i]
			self._i += 1
			return p
		return self._probes[-1]


class _Sink:
	"""Minimal recording action sink for the self-test."""

	def __init__(self, forbid_release=False):
		self.releases = []
		self.invokes = []
		self.approvals = []
		self.comments = []
		self.forbid_release = forbid_release

	def release_issue(self, issue_id, owner_id):
		from tools.ops.agent_liveness.watchdog import ReleaseForbidden

		if self.forbid_release:
			raise ReleaseForbidden("forced 403")
		self.releases.append((issue_id, owner_id))

	def invoke_heartbeat(self, agent_id):
		self.invokes.append(agent_id)

	def create_board_approval(self, *, issue_ids, title, summary, recommended_action, risks):
		approval = {"id": "approval-selftest", "title": title, "issueIds": list(issue_ids)}
		self.approvals.append(approval)
		return approval

	def post_comment(self, issue_id, body):
		self.comments.append((issue_id, body))


def _noop(_seconds: float) -> None:
	"""No-op sleep for the self-test so it runs instantly."""


# =========================================================================== #
# CLI
# =========================================================================== #
def build_arg_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(description="Agent-liveness recovery watchdog (FLO-968).")
	parser.add_argument("--once", action="store_true", help="run a single recovery pass (default)")
	parser.add_argument(
		"--dry-run",
		action="store_true",
		help="observe-only: detect + report, release/invoke/escalate nothing",
	)
	parser.add_argument(
		"--self-test",
		action="store_true",
		help="run the pure-logic self-test (no API, no mutations) and exit",
	)
	return parser


def main(argv: list[str] | None = None) -> int:
	args = build_arg_parser().parse_args(argv)

	if args.self_test:
		return self_test()

	if not args.dry_run:
		for required in ("PAPERCLIP_API_URL", "PAPERCLIP_API_KEY", "PAPERCLIP_COMPANY_ID"):
			if not os.environ.get(required):
				print(
					f"error: {required} is required for live recovery "
					"(use --self-test or --dry-run for the logic check).",
					file=sys.stderr,
				)
				return 2
	elif not os.environ.get("PAPERCLIP_API_URL"):
		print(
			"error: PAPERCLIP_API_URL is required even for --dry-run (detection reads the API).",
			file=sys.stderr,
		)
		return 2

	try:
		return run_once(dry_run=args.dry_run)
	except SystemExit:
		raise
	except Exception as exc:  # noqa: BLE001 - surface any runner failure to the caller
		print(f"error: watchdog pass failed: {exc}", file=sys.stderr)
		return 1


if __name__ == "__main__":
	sys.exit(main())
