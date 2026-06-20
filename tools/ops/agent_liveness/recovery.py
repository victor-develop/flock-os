"""
Agent liveness recovery — the generalization beyond the CEO (FLO-395).

The CEO liveness watchdog ([FLO-267](/FLO/issues/FLO-267)) proved the
detect → release → restart → escalate pattern, but it is CEO-specific: the
recovery owner is a hardcoded peer (the Software Architect). When the
Software Architect itself went silent on [FLO-365](/FLO/issues/FLO-365)
(~3h, 2026-06-20), the platform's stale-run detector correctly fired
([FLO-376](/FLO/issues/FLO-376)) but routed recovery back to the **same
stuck agent** — so the org's #2 agent was effectively offline with no
self-heal while its run slot was occupied. That is the gap this module
closes.

The fix is one rule, applied to the whole chain of command:

    recovery_owner(stuck_agent) = stuck_agent.manager
        Architect  -> CEO
        DevOps     -> Architect
        Frontend   -> Architect
        QA         -> Architect
        Backend    -> Architect
        CEO        -> None  (top of chain; board escalation is the backstop,
                             a configured peer — the Software Architect —
                             owns the release+restart attempts)

This module is **agent-agnostic** and **transport-agnostic**. It reuses the
CEO heartbeat module's run-detection primitives (:class:`HeartbeatRun`,
:func:`detect_silent_runs`, :data:`ACTIVE_RUN_STATUSES`) — those are already
generic in shape — and layers three things on top:

1. :func:`resolve_recovery_owner` — the chain-of-command resolution (the core
   fix; never returns the stuck agent itself).
2. :func:`classify_run` — the HEALTHY / SUSPICIOUS / STUCK / ABORT verdict
   from the runbook's age thresholds + the linked-issue terminal state + the
   zombie-run supersession test (FLO-419: a run superseded by a strictly newer
   non-coalesced base run is HEALTHY — the agent moved on).
3. :func:`plan_recovery` — the next action (NOOP / RELEASE_RESTART /
   ESCALATE_BOARD) given the verdict + prior attempt count, with exponential
   backoff and a max-attempts ceiling.

Import-clean: no bench, no Frappe, no network. The unit suite injects the
chain of command, a fake clock, and hand-built runs and asserts the verdict
+ recovery-owner math — including the canonical negative test that a
simulated silent Architect run self-heals (owner resolves to the CEO, never
to the Architect). The live Paperclip REST calls (release / heartbeat invoke
/ board approval) live in the watchdog routine procedure documented in
`docs/operations/agent-liveness-recovery-runbook.md`; this module is the
deterministic core that procedure obeys.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

# Reuse the CEO heartbeat module's run-detection primitives — they are
# agent-agnostic in shape (a HeartbeatRun is a run of any routine), so DRY
# means composing them, not duplicating them.
from tools.ops.ceo_heartbeat.monitor import HeartbeatRun


# ---------------------------------------------------------------------------- #
# Recovery-owner resolution — the core FLO-395 fix.
# ---------------------------------------------------------------------------- #
@dataclass(frozen=True)
class AgentNode:
	"""One node in the company chain of command.

	``reports_to`` is the manager's agent id, or ``None`` for the top of the
	chain (the CEO). The recovery owner for a stuck agent is its manager; the
	CEO has no manager, so its recovery is owned by a configured peer and the
	board is the escalation backstop.
	"""

	id: str
	name: str
	reports_to: str | None = None


@dataclass(frozen=True)
class ChainOfCommand:
	"""Index of the reporting tree, keyed by agent id.

	Built once per watchdog fire from ``GET /api/companies/{id}/agents`` (each
	agent carries ``reportsTo``). Resolution is a single map lookup — O(1) —
	and cycle-safe by construction (a well-formed chain has no cycles).
	"""

	_nodes: dict[str, AgentNode]

	@classmethod
	def from_agents(cls, agents: Iterable[AgentNode]) -> ChainOfCommand:
		"""Build a chain from an iterable of :class:`AgentNode` records."""
		return cls(_nodes={a.id: a for a in agents})

	def get(self, agent_id: str) -> AgentNode | None:
		"""Return the node for ``agent_id`` or ``None`` if unknown."""
		return self._nodes.get(agent_id)

	def manager_of(self, agent_id: str) -> AgentNode | None:
		"""The direct manager of ``agent_id`` (``None`` at the top of the chain).

		Defensive: an unknown agent id yields ``None`` (treated like the CEO —
		no in-chain manager, so the watchdog falls back to its configured peer
		owner and board escalation rather than silently wedging).
		"""
		node = self._nodes.get(agent_id)
		if node is None or node.reports_to is None:
			return None
		return self._nodes.get(node.reports_to)


def resolve_recovery_owner(
	stuck_agent_id: str,
	chain: ChainOfCommand,
	*,
	ceo_fallback_owner_id: str | None = None,
) -> str | None:
	"""The agent that should own recovery for a stuck run — **never the stuck agent itself**.

	This is the core FLO-395 fix. For any agent with a manager, the manager
	owns recovery (Architect → CEO; DevOps → Architect; …). For the CEO (top
	of chain, no manager), recovery is owned by ``ceo_fallback_owner_id`` —
	the configured peer that runs the CEO Liveness Watchdog routine (the
	Software Architect in this org) — and the board is the escalation
	backstop once attempts are exhausted.

	Returns ``None`` when either (a) the stuck agent is the CEO AND no
	fallback peer is configured, or (b) the agent id is unknown to the chain
	— both are misconfigurations the watchdog must surface, not swallow. The
	result is guaranteed to differ from ``stuck_agent_id`` when a manager or
	fallback exists — that invariant is pinned by the test suite and is the
	whole point of this module.
	"""
	node = chain.get(stuck_agent_id)
	if node is None:
		# Unknown agent — never silently route to the fallback; surface as None.
		return None
	manager = chain.manager_of(stuck_agent_id)
	if manager is not None:
		# A manager is, by construction, a different agent than the report.
		return manager.id
	# ``node`` is in the chain with no manager → top of chain (the CEO).
	# Hand recovery to the configured peer fallback (the CEO watchdog owner).
	if ceo_fallback_owner_id is not None and ceo_fallback_owner_id != stuck_agent_id:
		return ceo_fallback_owner_id
	return None


# ---------------------------------------------------------------------------- #
# Verdict thresholds + classification (matches the runbook constants).
# ---------------------------------------------------------------------------- #
# These mirror docs/operations/agent-liveness-recovery-runbook.md §Constants.
# A normal heartbeat/run is 5–10 min; T_SUSPICIOUS exceeds that + one cycle.
DEFAULT_T_SUSPICIOUS_MINUTES = 15.0
DEFAULT_T_STUCK_MINUTES = 20.0
DEFAULT_T_ESCALATE_MINUTES = 30.0
DEFAULT_T_ABORT_MINUTES = 45.0

DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BACKOFF_SECONDS: tuple[int, ...] = (30, 60, 120)

VERDICT_HEALTHY = "healthy"
VERDICT_SUSPICIOUS = "suspicious"
VERDICT_STUCK = "stuck"
VERDICT_ABORT = "abort"

ACTION_NOOP = "noop"
ACTION_RELEASE_RESTART = "release_restart"
ACTION_ESCALATE_BOARD = "escalate_board"


@dataclass(frozen=True)
class LivenessThresholds:
	"""The age thresholds (minutes) the verdict logic obeys.

	Field-for-field the runbook's §Constants table. Frozen so the watchdog can
	share one instance across a fire without it being mutated mid-procedure.
	"""

	suspicious_minutes: float = DEFAULT_T_SUSPICIOUS_MINUTES
	stuck_minutes: float = DEFAULT_T_STUCK_MINUTES
	escalate_minutes: float = DEFAULT_T_ESCALATE_MINUTES
	abort_minutes: float = DEFAULT_T_ABORT_MINUTES


def _age_minutes(triggered_at_epoch: float | None, now_epoch: float) -> float | None:
	if triggered_at_epoch is None:
		return None
	return max(0.0, (now_epoch - triggered_at_epoch) / 60.0)


@dataclass(frozen=True)
class RunVerdict:
	"""The classified state of one in-flight run against the runbook.

	``verdict`` drives :func:`plan_recovery`. ``recovery_owner_id`` is
	resolved up-front so the watchdog never has to re-derive it and so the
	test suite can pin the "owner ≠ stuck agent" invariant in one place.
	``reason`` is a short human-readable string for the watchdog's comment.
	"""

	verdict: str
	age_minutes: float
	stuck_agent_id: str
	recovery_owner_id: str | None
	linked_issue_id: str | None
	reason: str


def classify_run(
	run: HeartbeatRun,
	*,
	now_epoch: float,
	stuck_agent_id: str,
	chain: ChainOfCommand,
	thresholds: LivenessThresholds,
	linked_issue_is_terminal: bool = False,
	superseded_by_newer_base_run: bool = False,
	ceo_fallback_owner_id: str | None = None,
) -> RunVerdict:
	"""Classify one in-flight run into HEALTHY / SUSPICIOUS / STUCK / ABORT.

	Matches the runbook's Step 1 verdict table exactly:

	* linked issue terminal (``done``/``cancelled``) → **HEALTHY** (the run
		finished; the silent appearance is a stale signal).
	* superseded by a strictly newer non-coalesced base run → **HEALTHY**
		(FLO-419: ``coalesce_if_active`` only admits a fresh base run once the
		prior one is inactive, so a newer base run means the agent moved on and
		this in-flight record is a zombie, not a stuck run).
	* ``age < T_SUSPICIOUS`` → **HEALTHY** (within normal bounds).
	* ``T_SUSPICIOUS ≤ age < T_STUCK`` → **SUSPICIOUS** (log + watch).
	* ``age ≥ T_STUCK`` and the linked issue is still active → **STUCK**
		(recovery starts).
	* ``age ≥ T_ABORT`` → **ABORT** (stop automated retry; human/board only).

	``recovery_owner_id`` is resolved even for non-STUCK verdicts so the
	suspicious/abort comments route to the right owner; it is ``None`` only
	for an unmanaged top-of-chain agent with no configured fallback.

	``superseded_by_newer_base_run`` is computed by the caller via
	:func:`tools.ops.ceo_heartbeat.monitor.is_superseded_by_newer_base_run`
	(one owner for the rule — DRY) so this verdict and detection can never
	disagree on a zombie.
	"""
	owner_id = resolve_recovery_owner(stuck_agent_id, chain, ceo_fallback_owner_id=ceo_fallback_owner_id)
	age = _age_minutes(run.triggered_at_epoch, now_epoch)
	if age is None:
		return RunVerdict(
			verdict=VERDICT_HEALTHY,
			age_minutes=0.0,
			stuck_agent_id=stuck_agent_id,
			recovery_owner_id=owner_id,
			linked_issue_id=run.linked_issue_id,
			reason="run age unknown — treating as healthy",
		)

	if linked_issue_is_terminal:
		return RunVerdict(
			verdict=VERDICT_HEALTHY,
			age_minutes=age,
			stuck_agent_id=stuck_agent_id,
			recovery_owner_id=owner_id,
			linked_issue_id=run.linked_issue_id,
			reason="linked issue is terminal — run is not stuck",
		)

	if superseded_by_newer_base_run:
		return RunVerdict(
			verdict=VERDICT_HEALTHY,
			age_minutes=age,
			stuck_agent_id=stuck_agent_id,
			recovery_owner_id=owner_id,
			linked_issue_id=run.linked_issue_id,
			reason="superseded by a newer non-coalesced base run — agent moved on (FLO-419)",
		)

	if age >= thresholds.abort_minutes:
		return RunVerdict(
			verdict=VERDICT_ABORT,
			age_minutes=age,
			stuck_agent_id=stuck_agent_id,
			recovery_owner_id=owner_id,
			linked_issue_id=run.linked_issue_id,
			reason=f"age {age:.1f}m >= T_ABORT {thresholds.abort_minutes:.0f}m — stop automated retry",
		)

	if age >= thresholds.stuck_minutes:
		return RunVerdict(
			verdict=VERDICT_STUCK,
			age_minutes=age,
			stuck_agent_id=stuck_agent_id,
			recovery_owner_id=owner_id,
			linked_issue_id=run.linked_issue_id,
			reason=f"age {age:.1f}m >= T_STUCK {thresholds.stuck_minutes:.0f}m and linked issue active",
		)

	if age >= thresholds.suspicious_minutes:
		return RunVerdict(
			verdict=VERDICT_SUSPICIOUS,
			age_minutes=age,
			stuck_agent_id=stuck_agent_id,
			recovery_owner_id=owner_id,
			linked_issue_id=run.linked_issue_id,
			reason=f"age {age:.1f}m in suspicious window — watch",
		)

	return RunVerdict(
		verdict=VERDICT_HEALTHY,
		age_minutes=age,
		stuck_agent_id=stuck_agent_id,
		recovery_owner_id=owner_id,
		linked_issue_id=run.linked_issue_id,
		reason=f"age {age:.1f}m within normal bounds",
	)


# ---------------------------------------------------------------------------- #
# Recovery plan — the next action given the verdict + prior attempts.
# ---------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RecoveryPlan:
	"""The next action the watchdog takes for a classified run.

	* ``action`` — :data:`ACTION_NOOP`, :data:`ACTION_RELEASE_RESTART`, or
		:data:`ACTION_ESCALATE_BOARD`.
	* ``attempts_remaining`` — how many release+restart attempts are still
		budgeted (0 once exhausted → escalate).
	* ``next_backoff_seconds`` — delay before the next attempt, drawn from the
		exponential backoff schedule (None on NOOP / ESCALATE).
	* ``recovery_owner_id`` — the agent that executes the action (the stuck
		agent's manager; never the stuck agent).
	* ``reason`` — short human string for the watchdog comment / audit trail.
	"""

	action: str
	attempts_remaining: int
	next_backoff_seconds: int | None
	recovery_owner_id: str | None
	reason: str


def plan_recovery(
	verdict: RunVerdict,
	*,
	prior_attempts: int = 0,
	max_attempts: int = DEFAULT_MAX_ATTEMPTS,
	backoff_seconds: tuple[int, ...] = DEFAULT_BACKOFF_SECONDS,
	escalate_minutes: float = DEFAULT_T_ESCALATE_MINUTES,
) -> RecoveryPlan:
	"""Map a verdict + prior attempt count to the watchdog's next action.

	* HEALTHY / SUSPICIOUS → **NOOP** (suspicious is watch-only).
	* STUCK and ``prior_attempts < max_attempts`` → **RELEASE_RESTART** with
		the next backoff delay (0s on the first attempt, then the schedule).
	* STUCK and attempts exhausted, **or** age ≥ T_ESCALATE, **or** ABORT →
		**ESCALATE_BOARD** (stop retrying; do not hammer the agent).

	Backoff indexing: attempt N (1-based) sleeps ``backoff_seconds[N-1]``
	before firing when N > 1; the first attempt fires immediately. The
	schedule default ``(30, 60, 120)`` covers the 3-attempt ceiling.
	"""
	owner = verdict.recovery_owner_id

	if verdict.verdict in (VERDICT_HEALTHY, VERDICT_SUSPICIOUS):
		return RecoveryPlan(
			action=ACTION_NOOP,
			attempts_remaining=max(0, max_attempts - prior_attempts),
			next_backoff_seconds=None,
			recovery_owner_id=owner,
			reason=f"verdict {verdict.verdict} — no recovery action",
		)

	# STUCK or ABORT below this point.
	over_escalate = verdict.age_minutes >= escalate_minutes
	exhausted = prior_attempts >= max_attempts
	if verdict.verdict == VERDICT_ABORT or exhausted or over_escalate:
		return RecoveryPlan(
			action=ACTION_ESCALATE_BOARD,
			attempts_remaining=0,
			next_backoff_seconds=None,
			recovery_owner_id=owner,
			reason=(
				f"escalate: verdict={verdict.verdict} age={verdict.age_minutes:.1f}m "
				f"attempts={prior_attempts}/{max_attempts}"
			),
		)

	# STUCK with attempts remaining → release + restart with backoff.
	attempts_remaining = max(0, max_attempts - prior_attempts - 1)
	# First attempt (prior_attempts == 0) fires immediately; subsequent
	# attempts sleep backoff_seconds[prior_attempts-1] (30s, 60s, …).
	if prior_attempts <= 0:
		delay: int | None = 0
	elif backoff_seconds:
		delay = backoff_seconds[min(prior_attempts - 1, len(backoff_seconds) - 1)]
	else:
		delay = 0
	return RecoveryPlan(
		action=ACTION_RELEASE_RESTART,
		attempts_remaining=attempts_remaining,
		next_backoff_seconds=delay,
		recovery_owner_id=owner,
		reason=(f"release+restart: attempt {prior_attempts + 1}/{max_attempts} backoff={delay}s"),
	)


# ---------------------------------------------------------------------------- #
# Convenience: the full recovery-owner table for a chain (runbook §Coverage).
# ---------------------------------------------------------------------------- #
def recovery_owner_table(
	chain: ChainOfCommand,
	*,
	ceo_fallback_owner_id: str | None = None,
) -> tuple[tuple[str, str, str | None], ...]:
	"""Render the ``(agent_name, manager_name_or_self, recovery_owner_name)`` table.

	The runbook's §Coverage table is generated from this so the doc and the
	code cannot drift. ``manager_name`` is ``"—"`` for the top of chain.
	"""
	rows: list[tuple[str, str, str | None]] = []
	for node in chain._nodes.values():
		manager = chain.manager_of(node.id)
		manager_name = manager.name if manager else "—"
		owner_id = resolve_recovery_owner(node.id, chain, ceo_fallback_owner_id=ceo_fallback_owner_id)
		owner = chain.get(owner_id) if owner_id else None
		owner_name = owner.name if owner else ("board (no fallback)" if owner_id is None else "—")
		rows.append((node.name, manager_name, owner_name))
	# Stable order: CEO first, then everyone else alphabetical.
	rows.sort(key=lambda r: (r[1] == "—" and r[2] != "board (no fallback)", r[0]))
	return tuple(rows)
