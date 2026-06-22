"""
Agent-liveness watchdog — the live recovery runner (FLO-968).

``tools.ops.agent_liveness.recovery`` owns the *decision* (verdict + plan), and
``docs/operations/agent-liveness-recovery-runbook.md`` documents the *procedure*.
But a procedure an agent hand-executes with curl is fragile: the
[FLO-968](/FLO/issues/FLO-968) watchdog run that was monitoring the CEO failed
with a transient adapter error mid-run, leaving the liveness safeguard stranded
with no live path. This module is the deterministic **runner** that closes that
gap — one entry point that performs the runbook's Step 1–4 every fire, so the
recovery-owner agent invokes a script instead of hand-writing REST calls.

Layering (ports & adapters — same shape as the CEO heartbeat monitor):

    LivenessReader  (port)      <- PaperclipLivenessReader / ScriptedLivenessReader
    RecoveryActions (port)      <- PaperclipRecoveryActions / RecordingRecoveryActions
        -> run_watchdog()         <- the runbook Step 1–4 loop, pure given the ports
        -> WatchdogOutcome        <- the comment body + exit posture

``run_watchdog`` is pure given injected ports + a clock + a sleep callable: the
unit suite injects scripted probes and a recording action sink and asserts the
detect → release → restart → escalate loop, the recovery-owner invariant, and
the zombie / missing-disposition suppressions — without a network. The live
Paperclip REST wiring (release / heartbeat-invoke / board-approval / comment)
lives in the adapters here and is exercised end-to-end by the CLI in
``scripts/dev/agent-liveness-watchdog.py``.

Import-clean without a bench and without network: ``urllib`` is lazy-imported
inside the default HTTP callables, exactly like the sibling monitor module.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

# Reuse the decision core (FLO-395) + the shared run-detection primitives.
from tools.ops.agent_liveness.recovery import (
	ACTION_ESCALATE_BOARD,
	ACTION_NOOP,
	ACTION_RELEASE_RESTART,
	DEFAULT_BACKOFF_SECONDS,
	DEFAULT_MAX_ATTEMPTS,
	ChainOfCommand,
	LivenessThresholds,
	RunVerdict,
	classify_run,
	plan_recovery,
)
from tools.ops.ceo_heartbeat.monitor import (
	HeartbeatRun,
	is_superseded_by_newer_base_run,
)

# Action labels surfaced in the outcome (NOOP/RELEASE_RESTART come from the
# decision core; the runner adds the terminal RECOVERED + DRY_RUN outcomes).
ACTION_RECOVERED = "recovered"
ACTION_DRY_RUN = "dry_run"

# How long to wait after invoke_heartbeat for a fresh run to be admitted before
# re-detecting (matches the runbook Step 3 ``sleep(45s)`` admit window).
DEFAULT_ADMIT_WAIT_SECONDS = 45.0


# ---------------------------------------------------------------------------- #
# Errors the recovery actions may raise.
# ---------------------------------------------------------------------------- #
class RecoveryActionError(RuntimeError):
	"""A live recovery REST call failed (network / non-2xx / unexpected payload)."""


class ReleaseForbidden(RecoveryActionError):
	"""``release`` returned 401/403 — the watchdog lacks checkout authority.

	Per the runbook Step 3 scope note, a forbidden release skips the retry loop
	and escalates straight to the board: the automation cannot help, so a human
	must authorise force-release / pause-resume.
	"""


# ---------------------------------------------------------------------------- #
# Configuration + the liveness probe (one read of the watched agent's state).
# ---------------------------------------------------------------------------- #
@dataclass(frozen=True)
class WatchdogConfig:
	"""Everything the runner needs to be deterministic for one watched agent.

	``watched_agent_id`` is the agent whose liveness we guard. For the CEO (top
	of chain) ``ceo_fallback_owner_id`` is the configured peer that owns the
	release+restart attempts (the Software Architect in this org); for every
	other agent the manager — resolved live from the chain — is the owner.
	``watchdog_issue_id`` is the routine-execution issue this fire is running
	on (the comment target). ``project_id`` / ``goal_id`` tag escalations.

	Detection input: set **either** ``watched_routine_id`` (heartbeat-routine
	agents — liveness read from the routine's run history) **or**
	``watched_agent_name`` (assignment-driven agents — liveness read from the
	platform's open ``Review silent active run for {name}`` issue). The decision
	core (:func:`classify_run`) is agent-model-agnostic, so both paths feed the
	same :class:`LivenessProbe`; only the reader adapter differs.
	"""

	watched_agent_id: str
	# Detection input — exactly one of these two must be set (see ``build_config``):
	#   * ``watched_routine_id`` — the heartbeat-routine path (CEO/Architect):
	#     liveness is read from ``GET /api/routines/{id}/runs``.
	#   * ``watched_agent_name`` — the assignment-driven path (everyone else):
	#     liveness is read from the platform's open ``Review silent active run
	#     for {name}`` issue (runbook §Step 1). These agents have no heartbeat
	#     routine, so the stale-run review issue is the only liveness signal.
	watched_routine_id: str | None = None
	watched_agent_name: str | None = None
	ceo_fallback_owner_id: str | None = None
	watchdog_issue_id: str | None = None
	project_id: str | None = None
	goal_id: str | None = None
	thresholds: LivenessThresholds = field(default_factory=LivenessThresholds)
	max_attempts: int = DEFAULT_MAX_ATTEMPTS
	backoff_seconds: tuple[int, ...] = DEFAULT_BACKOFF_SECONDS
	admit_wait_seconds: float = DEFAULT_ADMIT_WAIT_SECONDS


@dataclass(frozen=True)
class LivenessProbe:
	"""One read of the watched agent's liveness — everything ``classify_run`` needs.

	``run`` is the most recent **base** run (``coalesced_into_run_id`` is null).
	``None`` when the agent has no in-flight run at all (healthy). ``all_runs``
	carries the recent window so the zombie-supersession test can run. The
	``linked_issue_*`` flags are pre-computed by the reader from the linked
	issue's status + ``activeRecoveryAction`` so the runner stays pure.
	"""

	run: HeartbeatRun | None
	all_runs: tuple[HeartbeatRun, ...]
	chain: ChainOfCommand
	linked_issue_is_terminal: bool
	successful_run_missing_disposition: bool


# ---------------------------------------------------------------------------- #
# Ports.
# ---------------------------------------------------------------------------- #
@runtime_checkable
class LivenessReader(Protocol):
	"""Port: read the watched agent's current liveness state."""

	def probe(self) -> LivenessProbe: ...


@runtime_checkable
class RecoveryActions(Protocol):
	"""Port: the mutating side effects the recovery loop performs.

	Each method maps to one runbook Step. ``release_issue`` must raise
	:class:`ReleaseForbidden` on 401/403 so the loop can escalate instead of
	retrying an action it is not authorised to take.
	"""

	def release_issue(self, issue_id: str, owner_id: str) -> None: ...

	def invoke_heartbeat(self, agent_id: str) -> None: ...

	def create_board_approval(
		self,
		*,
		issue_ids: list[str],
		title: str,
		summary: str,
		recommended_action: str,
		risks: list[str],
	) -> dict[str, Any]: ...

	def post_comment(self, issue_id: str, body: str) -> None: ...


# ---------------------------------------------------------------------------- #
# Pure runner — the runbook Step 1–4 loop.
# ---------------------------------------------------------------------------- #
@dataclass(frozen=True)
class WatchdogOutcome:
	"""What one watchdog fire did, in a shape the CLI can print + post.

	``action`` is one of :data:`ACTION_NOOP`, :data:`ACTION_RELEASE_RESTART`,
	:data:`ACTION_RECOVERED`, :data:`ACTION_ESCALATE_BOARD`, or
	:data:`ACTION_DRY_RUN`. ``attempts_made`` is the number of release+restart
	attempts performed this fire. ``comment_body`` is the ready-to-post
	runbook comment; ``audit`` is the line log the CLI prints to stdout.
	"""

	action: str
	verdict: RunVerdict | None
	attempts_made: int
	recovered: bool
	comment_body: str
	audit: tuple[str, ...]


def _outcome(
	action: str,
	verdict: RunVerdict | None,
	attempts: int,
	recovered: bool,
	body: str,
	audit: list[str],
) -> WatchdogOutcome:
	return WatchdogOutcome(
		action=action,
		verdict=verdict,
		attempts_made=attempts,
		recovered=recovered,
		comment_body=body,
		audit=tuple(audit),
	)


def _classify_probe(
	probe: LivenessProbe,
	*,
	now_epoch: float,
	config: WatchdogConfig,
) -> RunVerdict | None:
	"""Run the verdict on a probe, or ``None`` when nothing is in flight."""
	run = probe.run
	if run is None:
		return None
	superseded = is_superseded_by_newer_base_run(run, probe.all_runs)
	return classify_run(
		run,
		now_epoch=now_epoch,
		stuck_agent_id=config.watched_agent_id,
		chain=probe.chain,
		thresholds=config.thresholds,
		linked_issue_is_terminal=probe.linked_issue_is_terminal,
		superseded_by_newer_base_run=superseded,
		successful_run_missing_disposition=probe.successful_run_missing_disposition,
		ceo_fallback_owner_id=config.ceo_fallback_owner_id,
	)


def run_watchdog(
	config: WatchdogConfig,
	reader: LivenessReader,
	actions: RecoveryActions,
	*,
	now_epoch: float | None = None,
	clock: Callable[[], float] | None = None,
	sleep: Callable[[float], None] | None = None,
	prior_attempts: int = 0,
	dry_run: bool = False,
) -> WatchdogOutcome:
	"""Execute one watchdog fire — the runbook Step 1–4 procedure.

	Returns a :class:`WatchdogOutcome` describing what happened. Pure given the
	injected ports + clock + sleep: the unit suite drives scripted probes and a
	recording action sink with a fake clock + no-op sleep. The live CLI injects
	the Paperclip adapters, ``time.time``, and ``time.sleep``.

	Flow:

	* **Detect** (Step 1) — probe + classify. Nothing in flight, terminal,
		zombie (FLO-419), or missing-disposition (FLO-771) → HEALTHY → NOOP.
	* **Plan** — map the verdict to NOOP / RELEASE_RESTART / ESCALATE_BOARD via
		:func:`plan_recovery`.
	* **Recover** (Step 3) — release+invoke with exponential backoff, max
		``max_attempts``, re-detecting after each; stop on recovery or escalate.
		A forbidden release (403) jumps straight to escalation.
	* **Escalate** (Step 4) — open a board approval + post an incident comment.
	"""
	clk = clock
	if clk is None:
		# A fixed ``now_epoch`` seeds a constant clock so initial + re-detects
		# agree on age (tests pass ``now_epoch`` + a no-op sleep); default to
		# wall-clock for the live CLI.
		clk = (lambda: now_epoch) if now_epoch is not None else time.time
	now = clk()
	audit: list[str] = []

	probe = reader.probe()
	chain = probe.chain
	if chain.get(config.watched_agent_id) is None:
		# Unknown watched agent — surface rather than silently wedge.
		audit.append(f"unknown watched agent {config.watched_agent_id}; cannot resolve chain")
		return _outcome(ACTION_NOOP, None, 0, False, _comment_unknown_agent(config), audit)

	verdict = _classify_probe(probe, now_epoch=now, config=config)

	if dry_run:
		return _finish_dry_run(verdict, audit, config)

	if verdict is None:
		audit.append("no in-flight base run — healthy")
		return _outcome(ACTION_NOOP, None, 0, False, _comment_healthy_no_run(config), audit)

	plan = plan_recovery(
		verdict,
		prior_attempts=prior_attempts,
		max_attempts=config.max_attempts,
		backoff_seconds=config.backoff_seconds,
		escalate_minutes=config.thresholds.escalate_minutes,
	)
	audit.append(f"verdict={verdict.verdict} age={verdict.age_minutes:.1f}m → {plan.action} ({plan.reason})")

	# Step 1/2 — NOOP: healthy (incl. terminal/zombie/missing-disposition) or
	# suspicious (watch-only). Suspicious gets a watch comment so it is visible.
	if plan.action == ACTION_NOOP:
		if verdict.verdict == "suspicious":
			body = _comment_watch(config, verdict)
			_post(actions, config.watchdog_issue_id, body, audit)
		else:
			body = _comment_healthy(config, verdict)
		return _outcome(ACTION_NOOP, verdict, 0, False, body, audit)

	# Escalation without any attempt (abort / age>=T_ESCALATE / already exhausted).
	if plan.action == ACTION_ESCALATE_BOARD:
		approval = _escalate(actions, config, verdict, attempts=0, reason=plan.reason, audit=audit)
		body = _comment_escalate(config, verdict, attempts=0, approval=approval)
		_post(actions, config.watchdog_issue_id, body, audit)
		return _outcome(ACTION_ESCALATE_BOARD, verdict, 0, False, body, audit)

	# Step 3 — release + restart loop. Resolve the owner up front and assert the
	# FLO-395 invariant: the recovery owner is NEVER the stuck agent.
	owner = verdict.recovery_owner_id
	linked_id = verdict.linked_issue_id
	if owner is None or owner == config.watched_agent_id:
		audit.append(
			f"recovery owner resolved to {owner!r} (stuck={config.watched_agent_id}) — "
			"unsafe; escalating instead of recovering"
		)
		approval = _escalate(
			actions, config, verdict, attempts=0, reason="no safe recovery owner", audit=audit
		)
		body = _comment_escalate(
			config,
			verdict,
			attempts=0,
			approval=approval,
			extra="recovery owner could not be resolved safely",
		)
		_post(actions, config.watchdog_issue_id, body, audit)
		return _outcome(ACTION_ESCALATE_BOARD, verdict, 0, False, body, audit)

	return _release_restart_loop(
		config=config,
		reader=reader,
		actions=actions,
		verdict=verdict,
		owner=owner,
		linked_id=linked_id,
		clk=clk,
		sleep=sleep or time.sleep,
		prior_attempts=prior_attempts,
		audit=audit,
	)


def _release_restart_loop(
	*,
	config: WatchdogConfig,
	reader: LivenessReader,
	actions: RecoveryActions,
	verdict: RunVerdict,
	owner: str,
	linked_id: str | None,
	clk: Callable[[], float],
	sleep: Callable[[float], None],
	prior_attempts: int,
	audit: list[str],
) -> WatchdogOutcome:
	"""Runbook Step 3: release+invoke with backoff, max attempts, then escalate."""
	max_attempts = config.max_attempts
	attempts = prior_attempts
	current = verdict
	recovered = False
	forbidden = False

	while True:
		plan = plan_recovery(
			current,
			prior_attempts=attempts,
			max_attempts=max_attempts,
			backoff_seconds=config.backoff_seconds,
			escalate_minutes=config.thresholds.escalate_minutes,
		)
		if plan.action != ACTION_RELEASE_RESTART:
			# Exhausted or crossed T_ESCALATE/T_ABORT mid-loop → stop retrying.
			audit.append(f"stop retrying: {plan.reason}")
			break

		if plan.next_backoff_seconds:
			audit.append(f"backoff {plan.next_backoff_seconds}s before attempt {attempts + 1}")
			sleep(plan.next_backoff_seconds)

		# We are now committing attempt #attempts+1 (count it before acting, so
		# a forbidden release still records the attempt that was tried).
		attempts += 1

		if linked_id is None:
			audit.append("no linked issue to release — skipping release, invoking only")
		else:
			try:
				actions.release_issue(linked_id, owner)
				audit.append(f"released issue {linked_id} (owner={owner})")
			except ReleaseForbidden as exc:
				audit.append(f"release forbidden (403): {exc} — escalating")
				forbidden = True
				break

		actions.invoke_heartbeat(config.watched_agent_id)
		audit.append(f"invoked heartbeat for {config.watched_agent_id}")
		sleep(config.admit_wait_seconds)

		# Re-detect (Step 1) — recovered if a fresh base run appeared, the linked
		# issue went terminal, or the verdict is now healthy. ``clk()`` shares the
		# injected clock so age stays consistent across the loop.
		probe = reader.probe()
		new_verdict = _classify_probe(probe, now_epoch=clk(), config=config)
		if probe.run is None or probe.linked_issue_is_terminal:
			audit.append(f"recovered on attempt {attempts}: run cleared / linked issue terminal")
			recovered = True
			break
		if new_verdict is not None and (
			new_verdict.verdict == "healthy" or is_superseded_by_newer_base_run(probe.run, probe.all_runs)  # type: ignore[arg-type]
		):
			audit.append(f"recovered on attempt {attempts}: fresh healthy run admitted")
			recovered = True
			break
		if new_verdict is not None:
			current = new_verdict
			audit.append(f"attempt {attempts} did not recover — verdict={current.verdict}")

	if recovered:
		body = _comment_recovered(config, verdict, owner=owner, attempts=attempts, age=verdict.age_minutes)
		_post(actions, config.watchdog_issue_id, body, audit)
		return _outcome(ACTION_RECOVERED, verdict, attempts, True, body, audit)

	reason = "release forbidden (403)" if forbidden else f"exhausted {attempts}/{max_attempts} attempts"
	approval = _escalate(actions, config, verdict, attempts=attempts, reason=reason, audit=audit)
	body = _comment_escalate(config, verdict, attempts=attempts, approval=approval, forbidden=forbidden)
	_post(actions, config.watchdog_issue_id, body, audit)
	return _outcome(ACTION_ESCALATE_BOARD, verdict, attempts, False, body, audit)


# ---------------------------------------------------------------------------- #
# Side-effect helpers (swallow comment/approval failures so the action itself
# is never masked by a logging failure — mirrors the sibling watchdog).
# ---------------------------------------------------------------------------- #
def _post(actions: RecoveryActions, issue_id: str | None, body: str, audit: list[str]) -> None:
	if issue_id is None:
		audit.append("(no watchdog issue id — comment skipped)")
		return
	try:
		actions.post_comment(issue_id, body)
	except RecoveryActionError as exc:  # pragma: no cover - defensive
		audit.append(f"warn: could not post comment: {exc}")


def _escalate(
	actions: RecoveryActions,
	config: WatchdogConfig,
	verdict: RunVerdict,
	*,
	attempts: int,
	reason: str,
	audit: list[str],
) -> dict[str, Any]:
	issue_ids = [iid for iid in (config.watchdog_issue_id, verdict.linked_issue_id) if iid]
	title = f"[Liveness] {config.watched_agent_id} stuck run {verdict.age_minutes:.0f}m — authorise recovery"
	summary = (
		f"Automated recovery for watched agent `{config.watched_agent_id}` could not self-heal "
		f"after {attempts} attempt(s): {reason}. Base run age {verdict.age_minutes:.1f}m; "
		f"recovery owner resolved to `{verdict.recovery_owner_id}`."
	)
	recommended = "Authorise force-release / pause-resume of the stuck agent, then re-fire the watchdog."
	risks = [
		"Force-releasing a senior agent's checkout may interrupt in-flight work.",
		"Pause/resume has org-wide blast radius for a senior agent.",
	]
	try:
		approval = actions.create_board_approval(
			issue_ids=issue_ids,
			title=title,
			summary=summary,
			recommended_action=recommended,
			risks=risks,
		)
		audit.append(f"opened board approval: {approval.get('id') if isinstance(approval, dict) else '?'}")
		return approval if isinstance(approval, dict) else {}
	except RecoveryActionError as exc:  # pragma: no cover - defensive
		audit.append(f"warn: could not open board approval: {exc}")
		return {}


def _finish_dry_run(verdict: RunVerdict | None, audit: list[str], config: WatchdogConfig) -> WatchdogOutcome:
	if verdict is None:
		audit.append("dry-run: no in-flight base run — would NOOP")
	else:
		plan = plan_recovery(
			verdict, max_attempts=config.max_attempts, backoff_seconds=config.backoff_seconds
		)
		audit.append(
			f"dry-run: verdict={verdict.verdict} age={verdict.age_minutes:.1f}m "
			f"→ would {plan.action} ({plan.reason})"
		)
	return _outcome(ACTION_DRY_RUN, verdict, 0, False, "", audit)


# ---------------------------------------------------------------------------- #
# Comment-body builders (runbook Step 3/4 phrasing, kept in one place).
# ---------------------------------------------------------------------------- #
def _comment_healthy_no_run(config: WatchdogConfig) -> str:
	return (
		f"## Liveness check — healthy\n\n"
		f"- Watched agent: `{config.watched_agent_id}`\n"
		f"- No in-flight base run detected; nothing to recover.\n"
		f"- Procedure: `docs/operations/agent-liveness-recovery-runbook.md`\n"
		f"- Runner: `scripts/dev/agent-liveness-watchdog.py` ([FLO-968](/FLO/issues/FLO-968))"
	)


def _comment_healthy(config: WatchdogConfig, verdict: RunVerdict) -> str:
	return (
		f"## Liveness check — healthy\n\n"
		f"- Watched agent: `{config.watched_agent_id}`\n"
		f"- {verdict.reason}.\n"
		f"- Procedure: `docs/operations/agent-liveness-recovery-runbook.md`\n"
		f"- Runner: `scripts/dev/agent-liveness-watchdog.py` ([FLO-968](/FLO/issues/FLO-968))"
	)


def _comment_watch(config: WatchdogConfig, verdict: RunVerdict) -> str:
	return (
		f"## Liveness check — suspicious (watch)\n\n"
		f"- Watched agent: `{config.watched_agent_id}`\n"
		f"- {verdict.reason}; no recovery action yet.\n"
		f"- Linked issue: `{verdict.linked_issue_id}`\n"
		f"- Procedure: `docs/operations/agent-liveness-recovery-runbook.md`\n"
		f"- Runner: `scripts/dev/agent-liveness-watchdog.py` ([FLO-968](/FLO/issues/FLO-968))"
	)


def _comment_recovered(
	config: WatchdogConfig, verdict: RunVerdict, *, owner: str, attempts: int, age: float
) -> str:
	return (
		f"## Watchdog recovered `{config.watched_agent_id}`\n\n"
		f"- Recovered run `{verdict.linked_issue_id}` after ~{age:.0f}m via release+invoke "
		f"(attempt {attempts}/{config.max_attempts}; owner=`{owner}`).\n"
		f"- Procedure: `docs/operations/agent-liveness-recovery-runbook.md` §Step 3\n"
		f"- Runner: `scripts/dev/agent-liveness-watchdog.py` ([FLO-968](/FLO/issues/FLO-968))"
	)


def _comment_escalate(
	config: WatchdogConfig,
	verdict: RunVerdict,
	*,
	attempts: int,
	approval: dict[str, Any],
	forbidden: bool = False,
	extra: str | None = None,
) -> str:
	approval_id = approval.get("id") if isinstance(approval, dict) else None
	approval_line = (
		f"- Board approval: [{approval_id}](/FLO/approvals/{approval_id})"
		if approval_id
		else "- Board approval: (creation failed — see audit)"
	)
	suffix = f"\n- {extra}" if extra else ""
	return (
		f"## Watchdog escalating `{config.watched_agent_id}` to the board\n\n"
		f"- Automation exhausted after {attempts}/{config.max_attempts} attempt(s); "
		f"{'release was forbidden (403)' if forbidden else 'recovery did not self-heal'}. "
		f"Base run age {verdict.age_minutes:.1f}m; recovery owner=`{verdict.recovery_owner_id}`.\n"
		f"- Linked issue: `{verdict.linked_issue_id}`\n"
		f"{approval_line}\n"
		f"- Procedure: `docs/operations/agent-liveness-recovery-runbook.md` §Step 4\n"
		f"- Runner: `scripts/dev/agent-liveness-watchdog.py` ([FLO-968](/FLO/issues/FLO-968))"
		f"{suffix}"
	)


def _comment_unknown_agent(config: WatchdogConfig) -> str:
	return (
		f"## Liveness check — misconfiguration\n\n"
		f"- Watched agent `{config.watched_agent_id}` is not in the chain of command; "
		f"cannot resolve a recovery owner. No action taken.\n"
		f"- Runner: `scripts/dev/agent-liveness-watchdog.py` ([FLO-968](/FLO/issues/FLO-968))"
	)


# ---------------------------------------------------------------------------- #
# Test doubles — a scripted reader + a recording action sink.
# ---------------------------------------------------------------------------- #
class ScriptedLivenessReader:
	"""Return probes from a fixed queue (one per ``probe()`` call).

	For tests that model the release+restart loop, queue several probes: the
	initial stuck probe, then the post-invoke recovered probes. Repeating the
	last probe when the queue empties models a steady state.
	"""

	def __init__(self, probes: list[LivenessProbe]) -> None:
		self._probes = list(probes)
		self._i = 0

	def probe(self) -> LivenessProbe:
		if self._i < len(self._probes):
			p = self._probes[self._i]
			self._i += 1
			return p
		return self._probes[-1]


@dataclass
class RecordingRecoveryActions:
	"""Records every recovery call in order; optionally forces a release 403.

	Used both as the test double (assert which calls were made) and as the
	CLI's ``--dry-run`` sink (records + prints instead of hitting the API).
	"""

	releases: list[tuple[str, str]] = field(default_factory=list)
	invokes: list[str] = field(default_factory=list)
	approvals: list[dict[str, Any]] = field(default_factory=list)
	comments: list[tuple[str, str]] = field(default_factory=list)
	forbid_release: bool = False
	next_approval_id: str = "approval-test"
	echo: Callable[[str], None] | None = None

	def release_issue(self, issue_id: str, owner_id: str) -> None:
		if self.echo:
			self.echo(f"[dry-run] would release issue {issue_id} (owner={owner_id})")
		if self.forbid_release:
			raise ReleaseForbidden(f"release {issue_id} -> HTTP 403 (forced)")
		self.releases.append((issue_id, owner_id))

	def invoke_heartbeat(self, agent_id: str) -> None:
		if self.echo:
			self.echo(f"[dry-run] would invoke heartbeat for {agent_id}")
		self.invokes.append(agent_id)

	def create_board_approval(
		self,
		*,
		issue_ids: list[str],
		title: str,
		summary: str,
		recommended_action: str,
		risks: list[str],
	) -> dict[str, Any]:
		if self.echo:
			self.echo(f"[dry-run] would open board approval: {title}")
		approval = {"id": self.next_approval_id, "title": title, "issueIds": list(issue_ids)}
		self.approvals.append(approval)
		return approval

	def post_comment(self, issue_id: str, body: str) -> None:
		if self.echo:
			self.echo(f"[dry-run] would post comment on {issue_id}")
		self.comments.append((issue_id, body))


# ---------------------------------------------------------------------------- #
# Live Paperclip adapters.
# ---------------------------------------------------------------------------- #
@runtime_checkable
class HTTPGetter(Protocol):
	def __call__(self, url: str, headers: dict[str, str]) -> str | bytes: ...


@runtime_checkable
class HTTPPoster(Protocol):
	def __call__(self, url: str, headers: dict[str, str], body: dict[str, Any]) -> Any: ...


def default_http_get(url: str, headers: dict[str, str]) -> str | bytes:
	"""Default GET fetcher (``urllib``); lazy-imported so the module is import-clean."""
	from urllib.error import HTTPError, URLError
	from urllib.request import Request, urlopen

	req = Request(url, headers=headers, method="GET")
	try:
		with urlopen(req, timeout=15) as resp:
			return resp.read()
	except HTTPError as exc:
		raise RecoveryActionError(f"GET {url} -> HTTP {exc.code}: {exc.reason}") from exc
	except URLError as exc:
		raise RecoveryActionError(f"GET {url} -> network error: {exc.reason}") from exc


def default_http_post(url: str, headers: dict[str, str], body: dict[str, Any]) -> Any:
	"""Default POST writer (``urllib``); lazy-imported so the module is import-clean."""
	from urllib.error import HTTPError, URLError
	from urllib.request import Request, urlopen

	data = json.dumps(body).encode("utf-8")
	req = Request(
		url,
		data=data,
		headers={**headers, "Content-Type": "application/json", "Accept": "application/json"},
		method="POST",
	)
	try:
		with urlopen(req, timeout=20) as resp:
			raw = resp.read()
	except HTTPError as exc:
		raise RecoveryActionError(f"POST {url} -> HTTP {exc.code}: {exc.reason}") from exc
	except URLError as exc:
		raise RecoveryActionError(f"POST {url} -> network error: {exc.reason}") from exc
	if isinstance(raw, (bytes, bytearray)):
		raw = raw.decode("utf-8", errors="replace")
	return json.loads(raw) if raw else None


def _parse_iso8601(value: str | None) -> float | None:
	"""Parse a Paperclip ISO-8601 timestamp to epoch seconds (``None`` if absent)."""
	if not value:
		return None
	text = value.strip().replace("Z", "+00:00")
	if not text:
		return None
	try:
		from datetime import UTC, datetime

		dt = datetime.fromisoformat(text)
		if dt.tzinfo is None:
			dt = dt.replace(tzinfo=UTC)
		return dt.timestamp()
	except Exception:  # noqa: BLE001 - liveness must never raise on a bad ts
		return None


_TERMINAL_ISSUE_STATUSES = frozenset({"done", "cancelled"})

# The stale-run review issue body is a stable platform template, e.g.
# "Started at: 2026-06-20T15:51:21.034Z" (runbook §Step 1). Capture the
# timestamp token so assignment-driven detection can age the run.
_STARTED_AT_RE = re.compile(r"Started at:\s*([^\n\r]+)", re.IGNORECASE)


def _parse_started_at(description: str | None) -> str | None:
	"""Extract the run's ``Started at`` timestamp from the review issue body.

	Returns the raw token (e.g. ``2026-06-20T15:51:21.034Z``) for
	:func:`_parse_iso8601`, or ``None`` when the body has no such line — the
	caller then treats the run age as unknown (``classify_run`` -> healthy),
	never raising on a malformed body.
	"""
	if not description:
		return None
	match = _STARTED_AT_RE.search(description)
	return match.group(1).strip() if match else None


class PaperclipLivenessReader:
	"""Production :class:`LivenessReader` over the Paperclip REST API.

	Supports both detection paths from the runbook §Step 1. Set
	``watched_routine_id`` for the heartbeat path (CEO/Architect): liveness is
	read from ``GET /api/routines/{rid}/runs``, the latest non-coalesced base
	run plus the recent window (zombie-supersession test). Set
	``watched_agent_name`` for the assignment-driven path (everyone else): the
	open ``Review silent active run for {name}`` issue (filtered to non-terminal)
	is the only liveness signal — it carries the stuck run id (``originRunId``),
	started-at, and source issue (``parentId``); no open issue means the agent is
	healthy (the no-run case).

	Reads in common: ``GET /api/companies/{id}/agents`` (the chain of command,
	``reportsTo``); ``GET /api/issues/{linkedId}`` (the linked issue's status +
	``activeRecoveryAction`` — terminal + missing-disposition flags).
	"""

	def __init__(
		self,
		*,
		api_url: str,
		api_key: str,
		company_id: str,
		watched_agent_id: str,
		watched_routine_id: str | None = None,
		watched_agent_name: str | None = None,
		http_get: HTTPGetter | None = None,
		run_limit: int = 20,
	) -> None:
		if not watched_routine_id and not watched_agent_name:
			raise ValueError(
				"PaperclipLivenessReader needs a detection input: set watched_routine_id "
				"(heartbeat path) or watched_agent_name (assignment-driven path)."
			)
		self.api_url = api_url.rstrip("/")
		self.api_key = api_key
		self.company_id = company_id
		self.watched_agent_id = watched_agent_id
		self.watched_routine_id = watched_routine_id
		self.watched_agent_name = watched_agent_name
		self.http_get = http_get or default_http_get
		self.run_limit = run_limit

	def _headers(self) -> dict[str, str]:
		return {"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"}

	def _get(self, path: str) -> Any:
		raw = self.http_get(f"{self.api_url}{path}", self._headers())
		if isinstance(raw, (bytes, bytearray)):
			raw = raw.decode("utf-8", errors="replace")
		if isinstance(raw, str):
			return json.loads(raw) if raw else None
		return raw

	def _fetch_chain(self) -> ChainOfCommand:
		from tools.ops.agent_liveness.recovery import AgentNode

		payload = self._get(f"/api/companies/{self.company_id}/agents")
		agents = payload if isinstance(payload, list) else []
		nodes: list[AgentNode] = []
		for a in agents:
			if not isinstance(a, dict) or not a.get("id"):
				continue
			nodes.append(
				AgentNode(
					id=a["id"],
					name=str(a.get("name") or a.get("shortName") or a["id"]),
					reports_to=a.get("reportsTo"),
				)
			)
		return ChainOfCommand.from_agents(nodes)

	def _fetch_runs(self) -> tuple[HeartbeatRun, ...]:
		payload = self._get(f"/api/routines/{self.watched_routine_id}/runs?limit={int(self.run_limit)}")
		runs = payload if isinstance(payload, list) else []
		out: list[HeartbeatRun] = []
		for r in runs:
			if not isinstance(r, dict) or not r.get("id"):
				continue
			linked = r.get("linkedIssue") if isinstance(r.get("linkedIssue"), dict) else {}
			out.append(
				HeartbeatRun(
					id=r["id"],
					status=str(r.get("status") or "unknown"),
					triggered_at_epoch=_parse_iso8601(r.get("triggeredAt")) or 0.0,
					completed_at_epoch=_parse_iso8601(r.get("completedAt")),
					linked_issue_id=r.get("linkedIssueId"),
					linked_issue_identifier=linked.get("identifier"),
					linked_issue_title=linked.get("title"),
					linked_issue_status=linked.get("status"),
					coalesced_into_run_id=r.get("coalescedIntoRunId"),
					failure_reason=r.get("failureReason"),
				)
			)
		return tuple(out)

	def _linked_flags(self, linked_issue_id: str | None) -> tuple[bool, bool]:
		"""Return ``(is_terminal, successful_run_missing_disposition)``."""
		if not linked_issue_id:
			return False, False
		issue = self._get(f"/api/issues/{linked_issue_id}")
		if not isinstance(issue, dict):
			return False, False
		is_terminal = str(issue.get("status")) in _TERMINAL_ISSUE_STATUSES
		action = issue.get("activeRecoveryAction") or {}
		missing = (
			isinstance(action, dict)
			and action.get("kind") == "missing_disposition"
			and action.get("cause") == "successful_run_missing_state"
		)
		return is_terminal, missing

	def _fetch_stale_run_issue(self) -> HeartbeatRun | None:
		"""Assignment-driven detection (runbook §Step 1, alt path).

		Search the platform's open ``Review silent active run for {name}`` issue
		(the stale-run detector's signal for an agent with no heartbeat routine),
		filter to non-terminal statuses, and reconstruct the :class:`HeartbeatRun`
		the decision core consumes. ``None`` when no such issue is open — the
		agent is healthy, mirroring the heartbeat path's no-run case exactly.

		Field mapping: run id comes from the issue's ``originRunId`` (the silent
		run); started-at from the ``Started at:`` line in the issue body (the
		run's triggeredAt); the linked source issue from the issue's ``parentId``
		(the stuck run's source issue — the Step 3 release/reassign target).
		"""
		from urllib.parse import quote

		query = f"Review silent active run for {self.watched_agent_name}"
		payload = self._get(f"/api/companies/{self.company_id}/issues?q={quote(query)}")
		if isinstance(payload, dict):
			issues = payload.get("items") or payload.get("issues") or []
		else:
			issues = payload if isinstance(payload, list) else []
		open_issues = [
			i for i in issues if isinstance(i, dict) and str(i.get("status")) not in _TERMINAL_ISSUE_STATUSES
		]
		if not open_issues:
			return None
		# The most recent open review issue is the current liveness signal.
		review = max(open_issues, key=lambda i: i.get("createdAt") or "")
		run_id = review.get("originRunId") or review.get("originId")
		started = _parse_started_at(review.get("description"))
		return HeartbeatRun(
			id=str(run_id or "unknown"),
			status="running",
			triggered_at_epoch=_parse_iso8601(started) or 0.0,
			completed_at_epoch=None,
			linked_issue_id=review.get("parentId"),
		)

	def probe(self) -> LivenessProbe:
		chain = self._fetch_chain()
		if self.watched_routine_id:
			# Heartbeat-routine path (CEO/Architect): read the routine run history.
			runs = self._fetch_runs()
			base_runs = tuple(r for r in runs if not r.coalesced_into_run_id)
			latest = max(base_runs, key=lambda r: r.triggered_at_epoch) if base_runs else None
		else:
			# Assignment-driven path (everyone else): the open stale-run review
			# issue is the only liveness signal. No open issue => healthy noop,
			# exactly like the heartbeat path's no-run case.
			latest = self._fetch_stale_run_issue()
			runs = (latest,) if latest is not None else ()
		linked_id = latest.linked_issue_id if latest else None
		is_terminal, missing = self._linked_flags(linked_id)
		return LivenessProbe(
			run=latest,
			all_runs=runs,
			chain=chain,
			linked_issue_is_terminal=is_terminal,
			successful_run_missing_disposition=missing,
		)


class PaperclipRecoveryActions:
	"""Production :class:`RecoveryActions` over the Paperclip REST API.

	Implements the runbook's release (Step 3), heartbeat-invoke (Step 3),
	board-approval (Step 4), and comment mutations. The run JWT
	(``X-Paperclip-Run-Id``) is attached to every mutating call so the watchdog
	fire is traceable, exactly like a heartbeat-driven action.
	"""

	def __init__(
		self,
		*,
		api_url: str,
		api_key: str,
		company_id: str,
		run_id: str | None = None,
		http_post: HTTPPoster | None = None,
	) -> None:
		self.api_url = api_url.rstrip("/")
		self.api_key = api_key
		self.company_id = company_id
		self.run_id = run_id
		self.http_post = http_post or default_http_post

	def _headers(self) -> dict[str, str]:
		h = {"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"}
		if self.run_id:
			h["X-Paperclip-Run-Id"] = self.run_id
		return h

	def _post(self, path: str, body: dict[str, Any]) -> Any:
		return self.http_post(f"{self.api_url}{path}", self._headers(), body)

	def release_issue(self, issue_id: str, owner_id: str) -> None:
		try:
			self._post(f"/api/issues/{issue_id}/release", {"agentId": owner_id})
		except RecoveryActionError as exc:
			if "HTTP 401" in str(exc) or "HTTP 403" in str(exc):
				raise ReleaseForbidden(str(exc)) from exc
			raise

	def invoke_heartbeat(self, agent_id: str) -> None:
		self._post(f"/api/agents/{agent_id}/heartbeat/invoke", {})

	def create_board_approval(
		self,
		*,
		issue_ids: list[str],
		title: str,
		summary: str,
		recommended_action: str,
		risks: list[str],
	) -> dict[str, Any]:
		body = {
			"type": "request_board_approval",
			"issueIds": list(issue_ids),
			"payload": {
				"title": title,
				"summary": summary,
				"recommendedAction": recommended_action,
				"risks": list(risks),
			},
		}
		result = self._post(f"/api/companies/{self.company_id}/approvals", body)
		return result if isinstance(result, dict) else {}

	def post_comment(self, issue_id: str, body: str) -> None:
		self._post(f"/api/issues/{issue_id}/comments", {"comment": body})


__all__ = [
	"ACTION_DRY_RUN",
	"ACTION_RECOVERED",
	"DEFAULT_ADMIT_WAIT_SECONDS",
	"HTTPGetter",
	"HTTPPoster",
	"LivenessProbe",
	"LivenessReader",
	"PaperclipLivenessReader",
	"PaperclipRecoveryActions",
	"RecoveryActionError",
	"RecoveryActions",
	"RecordingRecoveryActions",
	"ReleaseForbidden",
	"ScriptedLivenessReader",
	"WatchdogConfig",
	"WatchdogOutcome",
	"default_http_get",
	"default_http_post",
	"run_watchdog",
]
