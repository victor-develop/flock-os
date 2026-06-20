"""
CEO heartbeat monitoring + observability (FLO-266).

The CEO silent-run incident ([FLO-264](/FLO/issues/FLO-264)) went undetected for
~1 hour because nothing was watching the CEO's heartbeat runs. The CEO is the
top of the chain of command, so a stuck CEO is a single point of failure for the
whole company — silent runs must surface in *minutes*, not hours.

This module owns the **detection + observability** half of the fix (the sibling
issues own the other halves: [FLO-265](/FLO/issues/FLO-265) timeout enforcement,
[FLO-267](/FLO/issues/FLO-267) recovery/restart). It is the Paperclip-control-
plane analogue of :mod:`flock_os.telemetry`: where that module watches the
Frappe *runtime* (Redis / MariaDB / bulk latency), this one watches the
*autonomous organization* — is the CEO's heartbeat firing on time, are individual
runs going silent, and what is the CEO doing right now.

Signals (all derived from the Paperclip control plane, no bench needed):

* **Liveness** — the CEO agent's ``status`` / ``lastHeartbeatAt`` and the
  org-chain health. A CEO that has not heartbeated within the run cadence is the
  first silent-run smoke signal.
* **Silent / stuck run detection** — a routine run whose status is non-terminal
  (``issue_created``) and whose ``completedAt`` is still null is in flight; once
  its age passes the warning / critical thresholds it becomes a silent-run alert.
  This is exactly the shape of the FLO-264 incident (a run that sat in
  ``issue_created`` for >1h without completing). **Zombie run-records** — an old
  in-flight run left behind after the agent admitted a newer base run (its linked
  issue transitioned to ``blocked``/``in_review``/``done``) — are suppressed via
  :func:`is_superseded_by_newer_base_run` (FLO-419) so a healthy, moved-on agent
  does not raise a false silent-run alert.
* **Completion-time trends** — p50 / p95 / max of completed-run durations, so a
  creeping slowdown is visible before it becomes a silent run.

Architecture (ports & adapters — same layering as :mod:`flock_os.telemetry`)::

    PaperclipCEOHeartbeatSource  (port)      <- production: Paperclip REST API
    StaticCEOHeartbeatSource                   <- in-memory, for the unit suite
        -> detect_silent_runs()                <- pure, threshold logic
        -> compute_completion_stats()          <- pure, duration trends
        -> evaluate_health()                   <- pure, the CEOHealthSnapshot
        -> snapshot.as_prometheus()            <- dashboard / scrape surface
    CEOHeartbeatMonitor                        <- orchestrates one scrape

Transport-agnostic + import-clean without a bench and without network: the
production source takes an injectable HTTP fetcher (defaults to ``urllib``) so
the unit suite injects a fake fetcher and asserts the detection math without
touching the live API.
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC
from typing import Any, Protocol, runtime_checkable

# ---------------------------------------------------------------------------- #
# Thresholds + severity (FLO-266 acceptance criteria).
# ---------------------------------------------------------------------------- #
# Incident analysis (docs/architecture/ceo-silent-run-analysis.md) target:
# alert (warning) at 15 minutes of silence, escalate (critical) at 30 minutes.
DEFAULT_WARNING_SILENCE_MINUTES = 15
"""Age at which an in-flight run becomes a WARNING silent-run alert (FLO-266)."""

DEFAULT_CRITICAL_SILENCE_MINUTES = 30
"""Age at which an in-flight run becomes a CRITICAL silent-run alert (FLO-266)."""

# The CEO heartbeat cadence (manifest.json: ``CEO @ */15 * * * *``). Used as the
# liveness-stale budget when no explicit completion data is available.
DEFAULT_HEARTBEAT_CADENCE_MINUTES = 15

# Run statuses that are still in flight (not yet reached a terminal state). A
# run in one of these states with a null ``completedAt`` is a candidate silent
# run. ``coalesce_if_active`` concurrency means a coalesced run is NOT silent —
# it is a skipped overlap that points at the real active run via
# ``coalescedIntoRunId``.
ACTIVE_RUN_STATUSES: frozenset[str] = frozenset({"issue_created", "running", "active", "pending", "queued"})

# Terminal statuses — a run here is done, success or failure, and is no longer a
# silent-run candidate (it may still be a *failure* trend signal).
TERMINAL_RUN_STATUSES: frozenset[str] = frozenset(
	{"completed", "succeeded", "failed", "cancelled", "coalesced", "skipped"}
)

# Exit codes the CLI maps severity to (UNIX convention: 0 ok, 1 warn, 2 crit).
EXIT_OK = 0
EXIT_WARNING = 1
EXIT_CRITICAL = 2
EXIT_CONFIG_ERROR = 3

# Severity labels.
SEVERITY_OK = "ok"
SEVERITY_WARNING = "warning"
SEVERITY_CRITICAL = "critical"


def _parse_iso8601_to_epoch(value: str | float | int | None) -> float | None:
	"""Parse an ISO-8601 timestamp (or epoch seconds) to epoch seconds.

	Paperclip timestamps are ISO-8601 with a trailing ``Z`` (UTC). Returns
	``None`` for missing/empty input. Accepts a bare epoch number unchanged.
	Defensive: any parse failure yields ``None`` rather than raising — a bad
	timestamp must never break a liveness scrape.
	"""
	if value is None or value == "":
		return None
	if isinstance(value, (int, float)):
		return float(value)
	try:
		s = str(value).strip()
		if not s:
			return None
		# Normalise the trailing Z (UTC) to an explicit offset so fromisoformat
		# (pre-3.11 strictness) accepts it on every supported Python.
		if s.endswith("Z"):
			s = s[:-1] + "+00:00"
		from datetime import datetime

		dt = datetime.fromisoformat(s)
		if dt.tzinfo is None:
			# Assume UTC for a naive timestamp (Paperclip always emits UTC).

			dt = dt.replace(tzinfo=UTC)
		return dt.timestamp()
	except Exception:  # noqa: BLE001 - liveness scrape must never raise
		return None


def _age_minutes(triggered_at_epoch: float | None, now_epoch: float) -> float | None:
	"""Minutes between ``triggered_at_epoch`` and ``now_epoch`` (``None`` if unknown)."""
	if triggered_at_epoch is None:
		return None
	return max(0.0, (now_epoch - triggered_at_epoch) / 60.0)


# ---------------------------------------------------------------------------- #
# Pure data records (snapshots of one run / the CEO state).
# ---------------------------------------------------------------------------- #
@dataclass(frozen=True)
class HeartbeatRun:
	"""One CEO heartbeat routine run, normalised to the shape detection needs.

	Carries only the fields the monitor reasons over so the unit suite can build
	runs by hand without mirroring the full Paperclip run object. ``status`` is
	the Paperclip routine-run status (``issue_created`` / ``completed`` /
	``failed`` / ``coalesced`` / ...). ``completed_at_epoch`` is ``None`` for an
	in-flight run. ``linked_issue_identifier`` (e.g. ``FLO-263``) is what the CEO
	is currently working on — the "current operation" surface.
	"""

	id: str
	status: str
	triggered_at_epoch: float
	completed_at_epoch: float | None
	linked_issue_id: str | None = None
	linked_issue_identifier: str | None = None
	linked_issue_title: str | None = None
	linked_issue_status: str | None = None
	coalesced_into_run_id: str | None = None
	failure_reason: str | None = None


@dataclass(frozen=True)
class CEOAgentState:
	"""The CEO agent's liveness fields at one instant."""

	id: str
	name: str
	status: str
	last_heartbeat_at_epoch: float | None
	updated_at_epoch: float | None
	org_chain_health_status: str | None = None
	org_chain_health_reason: str | None = None


@dataclass(frozen=True)
class SilentRunAlert:
	"""A single in-flight run that crossed a silence threshold (FLO-266).

	``severity`` is :data:`SEVERITY_WARNING` once age >= warning minutes and
	:data:`SEVERITY_CRITICAL` once age >= critical minutes. ``age_minutes`` is
	the run's age at evaluation time; ``run`` is the underlying record so the
	alert payload can name what the CEO is stuck on.
	"""

	run: HeartbeatRun
	age_minutes: float
	severity: str


@dataclass(frozen=True)
class CompletionStats:
	"""Duration trends over the completed runs in the window (FLO-266).

	``durations_minutes`` is the sorted sample the percentiles were computed
	from (handy for the dashboard sparkline). ``failure_count`` / ``total`` feed
	the success-rate gauge.
	"""

	count: int
	failure_count: int
	p50_minutes: float | None
	p95_minutes: float | None
	max_minutes: float | None
	mean_minutes: float | None
	durations_minutes: tuple[float, ...] = ()


@dataclass(frozen=True)
class CEOHealthSnapshot:
	"""One complete read of CEO heartbeat health (the dashboard / scrape surface).

	* ``severity`` — the overall CEO health (:data:`SEVERITY_OK` /
		:data:`SEVERITY_WARNING` / :data:`SEVERITY_CRITICAL`). Driven by silent-run
		alerts first, then liveness staleness, then org-chain health.
	* ``agent`` — the CEO liveness fields.
	* ``active_runs`` — every in-flight run (silent or not), so the dashboard can
		show "what the CEO is doing right now".
	* ``silent_run_alerts`` — the subset of active runs that crossed a threshold.
	* ``completion_stats`` — duration trends over completed runs in the window.
	* ``current_operation`` — the linked issue of the most-recent active run, the
		human-readable answer to "what is the CEO doing right now".
	"""

	taken_at_epoch: float
	severity: str
	agent: CEOAgentState
	active_runs: tuple[HeartbeatRun, ...]
	silent_run_alerts: tuple[SilentRunAlert, ...]
	completion_stats: CompletionStats
	current_operation: HeartbeatRun | None
	runs_inspected: int


# ---------------------------------------------------------------------------- #
# Source port + adapters.
# ---------------------------------------------------------------------------- #
@runtime_checkable
class CEOHeartbeatSource(Protocol):
	"""Port: snapshot the CEO agent state + recent routine runs.

	``run_limit`` bounds the recent-runs window (the dashboard only needs the
	last ~50 runs to compute trends + see the active run).
	"""

	def fetch(self, run_limit: int = 50) -> tuple[CEOAgentState, tuple[HeartbeatRun, ...]]: ...


class StaticCEOHeartbeatSource:
	"""In-memory :class:`CEOHeartbeatSource` for unit tests + local smoke.

	Holds a frozen ``(agent, runs)`` pair and returns it verbatim, so the
	detection math is exercised against hand-built data with no network.
	"""

	def __init__(
		self,
		agent: CEOAgentState,
		runs: tuple[HeartbeatRun, ...] = (),
	) -> None:
		self.agent = agent
		self.runs = tuple(runs)

	def fetch(self, run_limit: int = 50) -> tuple[CEOAgentState, tuple[HeartbeatRun, ...]]:
		return self.agent, self.runs[-run_limit:] if run_limit < len(self.runs) else self.runs


class PaperclipCEOHeartbeatSource:
	"""Production source: the Paperclip control-plane REST API.

	Reads:

	* ``GET /api/agents/{ceo_agent_id}`` — liveness (status, lastHeartbeatAt,
		orgChainHealth).
	* ``GET /api/companies/{companyId}/routines`` then
		``GET /api/routines/{routineId}/runs?limit=N`` — the CEO heartbeat routine
		run history (status / triggeredAt / completedAt / linkedIssue).

	The HTTP layer is an injectable callable (``http_get``) so the unit suite can
	drive this adapter against canned responses with zero network. The default
	fetcher is :func:`urllib.request.urlopen`; any error is raised to the caller
	(the monitor CLI decides exit posture). Only ``GET`` is used — the source
	never mutates Paperclip state.
	"""

	def __init__(
		self,
		*,
		api_url: str,
		api_key: str,
		company_id: str,
		ceo_agent_id: str,
		ceo_routine_id: str | None = None,
		http_get: HTTPGetter | None = None,
		default_run_limit: int = 50,
	) -> None:
		self.api_url = api_url.rstrip("/")
		self.api_key = api_key
		self.company_id = company_id
		self.ceo_agent_id = ceo_agent_id
		self.ceo_routine_id = ceo_routine_id
		self.http_get = http_get or default_http_get
		self.default_run_limit = default_run_limit

	def _get(self, path: str) -> Any:
		url = f"{self.api_url}{path}"
		headers = {"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"}
		raw = self.http_get(url, headers)
		if isinstance(raw, (bytes, bytearray)):
			raw = raw.decode("utf-8", errors="replace")
		if isinstance(raw, str):
			return json.loads(raw) if raw else None
		return raw

	def _resolve_routine_id(self) -> str:
		"""Find the CEO heartbeat routine id if not configured up front.

		Selects the active routine whose assignee is the CEO. Cached on the
		instance after the first call.
		"""
		if self.ceo_routine_id:
			return self.ceo_routine_id
		payload = self._get(f"/api/companies/{self.company_id}/routines")
		routines = payload if isinstance(payload, list) else []
		for r in routines:
			if not isinstance(r, dict):
				continue
			if r.get("assigneeAgentId") == self.ceo_agent_id and r.get("status") == "active":
				self.ceo_routine_id = r.get("id")
				break
		if not self.ceo_routine_id:
			raise RuntimeError(
				f"No active routine assigned to CEO agent {self.ceo_agent_id} "
				f"in company {self.company_id}; set FLO_CEO_ROUTINE_ID explicitly."
			)
		return self.ceo_routine_id

	def fetch(self, run_limit: int = 50) -> tuple[CEOAgentState, tuple[HeartbeatRun, ...]]:
		agent = self._fetch_agent()
		runs = self._fetch_runs(run_limit)
		return agent, runs

	def _fetch_agent(self) -> CEOAgentState:
		doc = self._get(f"/api/agents/{self.ceo_agent_id}")
		if not isinstance(doc, dict):
			raise RuntimeError(f"Unexpected agent payload for {self.ceo_agent_id}")
		chain = doc.get("orgChainHealth") or {}
		return CEOAgentState(
			id=doc.get("id", self.ceo_agent_id),
			name=doc.get("name") or "CEO",
			status=str(doc.get("status") or "unknown"),
			last_heartbeat_at_epoch=_parse_iso8601_to_epoch(doc.get("lastHeartbeatAt")),
			updated_at_epoch=_parse_iso8601_to_epoch(doc.get("updatedAt")),
			org_chain_health_status=chain.get("status") if isinstance(chain, dict) else None,
			org_chain_health_reason=chain.get("reason") if isinstance(chain, dict) else None,
		)

	def _fetch_runs(self, run_limit: int) -> tuple[HeartbeatRun, ...]:
		routine_id = self._resolve_routine_id()
		payload = self._get(f"/api/routines/{routine_id}/runs?limit={int(run_limit)}")
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
					triggered_at_epoch=_parse_iso8601_to_epoch(r.get("triggeredAt")) or 0.0,
					completed_at_epoch=_parse_iso8601_to_epoch(r.get("completedAt")),
					linked_issue_id=r.get("linkedIssueId"),
					linked_issue_identifier=linked.get("identifier"),
					linked_issue_title=linked.get("title"),
					linked_issue_status=linked.get("status"),
					coalesced_into_run_id=r.get("coalescedIntoRunId"),
					failure_reason=r.get("failureReason"),
				)
			)
		return tuple(out)


# ---------------------------------------------------------------------------- #
# Alert issuance — idempotent Paperclip alert-issue creation (FLO-266).
# ---------------------------------------------------------------------------- #
@runtime_checkable
class HTTPPoster(Protocol):
	"""Port: a minimal ``POST -> body`` JSON writer (the alert side)."""

	def __call__(self, url: str, headers: dict[str, str], body: dict[str, Any]) -> Any: ...


def default_http_post(url: str, headers: dict[str, str], body: dict[str, Any]) -> Any:
	"""Default :class:`HTTPPoster`: ``urllib.request.urlopen`` with a JSON body.

	Lazy-imports ``urllib`` so the module stays import-clean. Raises
	:class:`RuntimeError` on non-2xx / network error so the watchdog can surface
	a config/network failure instead of silently dropping an alert.
	"""
	import json as _json
	from urllib.error import HTTPError, URLError
	from urllib.request import Request, urlopen

	data = _json.dumps(body).encode("utf-8")
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
		raise RuntimeError(f"POST {url} -> HTTP {exc.code}: {exc.reason}") from exc
	except URLError as exc:
		raise RuntimeError(f"POST {url} -> network error: {exc.reason}") from exc
	if isinstance(raw, (bytes, bytearray)):
		raw = raw.decode("utf-8", errors="replace")
	return json.loads(raw) if raw else None


@dataclass
class PaperclipAlertIssuer:
	"""Open idempotent Paperclip alert issues for silent CEO runs (FLO-266).

	Routing (Rule #1 — never page a human for what an agent could do): alerts go
	to an *agent* owner, defaulting to the Software Architect (the CEO's de-facto
	recovery owner until [FLO-267](/FLO/issues/FLO-267) ships auto-restart; the
	architect handled the [FLO-264](/FLO/issues/FLO-264) incident).

	Idempotency: before creating an alert for a run, ``GET /api/companies/{id}/
	issues?q={runId}`` searches for an existing open issue that already mentions
	that run id. Only runs without a matching open alert get a new issue, so a
	5-minute poll cadence never stacks duplicates. The run id is embedded in both
	the search query and the title, making the dedupe key self-evident.
	"""

	api_url: str
	api_key: str
	company_id: str
	escalation_agent_id: str
	parent_issue_id: str | None = None
	project_id: str | None = None
	http_get: HTTPGetter | None = None  # type: ignore[assignment]
	http_post: HTTPPoster | None = None  # type: ignore[assignment]

	def _headers(self) -> dict[str, str]:
		return {"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"}

	def _get(self, path: str) -> Any:
		getter = self.http_get or default_http_get
		url = f"{self.api_url.rstrip('/')}{path}"
		raw = getter(url, self._headers())
		if isinstance(raw, (bytes, bytearray)):
			raw = raw.decode("utf-8", errors="replace")
		if isinstance(raw, str):
			return json.loads(raw) if raw else None
		return raw

	def _post(self, path: str, body: dict[str, Any]) -> Any:
		poster = self.http_post or default_http_post
		url = f"{self.api_url.rstrip('/')}{path}"
		return poster(url, self._headers(), body)

	def _has_open_alert(self, run_id: str) -> bool:
		"""True if an open (non-terminal) issue already references ``run_id``.

		Defensive: a failed search is treated as "no existing alert" (returns
		``False``) — better to duplicate an alert than to silently drop one
		because the search endpoint was briefly unavailable.
		"""
		from urllib.parse import quote

		try:
			payload = self._get(f"/api/companies/{self.company_id}/issues?q={quote(run_id)}")
		except Exception:  # noqa: BLE001 - search must never block a create
			return False
		if isinstance(payload, list):
			issues = payload
		elif isinstance(payload, dict):
			issues = payload.get("items") or []
		else:
			issues = []
		for issue in issues:
			if not isinstance(issue, dict):
				continue
			if issue.get("status") in {"done", "cancelled"}:
				continue
			# Match the run id in the title, description, or identifier so a
			# prior alert for the same silent run dedupes.
			haystack = " ".join(str(issue.get(k, "")) for k in ("title", "description", "identifier"))
			if run_id in haystack:
				return True
		return False

	def _alert_body(self, alert: SilentRunAlert) -> dict[str, Any]:
		run = alert.run
		what = run.linked_issue_identifier or run.linked_issue_title or run.id
		severity_label = alert.severity.upper()
		title = f"[{severity_label}] CEO silent heartbeat run {run.id} ({alert.age_minutes:.0f}m)"
		description = (
			f"Detected by the CEO heartbeat monitor (FLO-266): a CEO heartbeat "
			f"routine run is in flight beyond the {alert.severity} silence "
			f"budget.\n\n"
			f"- **Run id**: `{run.id}`\n"
			f"- **Severity**: {severity_label} (age {alert.age_minutes:.1f}m; "
			f"warning>=15m, critical>=30m)\n"
			f"- **Status**: `{run.status}` (completedAt is null)\n"
			f"- **Current operation**: {what}\n"
			f"- **Linked issue**: {run.linked_issue_identifier or 'n/a'} "
			f"({run.linked_issue_status or 'n/a'})\n\n"
			f"Recovery: see `docs/operations/ceo-heartbeat-monitoring.md`. "
			f"Auto-restart is owned by [FLO-267](/FLO/issues/FLO-267)."
		)
		body: dict[str, Any] = {
			"title": title,
			"description": description,
			"assigneeAgentId": self.escalation_agent_id,
			"status": "blocked",
			"priority": "critical" if alert.severity == SEVERITY_CRITICAL else "high",
			"labels": ["ceo-heartbeat", "silent-run", f"run:{run.id}"],
		}
		if self.parent_issue_id:
			body["parentId"] = self.parent_issue_id
		if self.project_id:
			body["projectId"] = self.project_id
		return body

	def issue_for(
		self,
		alerts: Sequence[SilentRunAlert] | tuple[SilentRunAlert, ...],
	) -> tuple[dict[str, Any], ...]:
		"""Create one alert issue per silent run that lacks an open alert.

		Returns the created issue payloads (empty when all alerts already had an
		open issue — the idempotent no-op path). A network/API failure on any
		single create is raised so the watchdog can surface it; the search step
		is defensive and treats a failed search as "no existing alert" (better to
		duplicate an alert than to silently drop one).
		"""
		alerts_t = alerts if isinstance(alerts, tuple) else tuple(alerts)
		created: list[dict[str, Any]] = []
		for alert in alerts_t:
			if self._has_open_alert(alert.run.id):
				continue
			payload = self._post(f"/api/companies/{self.company_id}/issues", self._alert_body(alert))
			if isinstance(payload, dict):
				created.append(payload)
		return tuple(created)


# ---------------------------------------------------------------------------- #
# HTTP fetcher port + default urllib implementation.
# ---------------------------------------------------------------------------- #
@runtime_checkable
class HTTPGetter(Protocol):
	"""Port: a minimal ``GET -> body`` fetcher (str | bytes)."""

	def __call__(self, url: str, headers: dict[str, str]) -> str | bytes: ...


def default_http_get(url: str, headers: dict[str, str]) -> str | bytes:
	"""Default :class:`HTTPGetter`: ``urllib.request.urlopen`` with a short timeout.

	Lazy-imports ``urllib`` so importing this module never requires the network
	and never wires a global opener at import time. Raises on non-2xx so the CLI
	can surface a config/network error instead of silently degrading.
	"""
	from urllib.error import HTTPError, URLError
	from urllib.request import Request, urlopen

	req = Request(url, headers=headers, method="GET")
	try:
		with urlopen(req, timeout=15) as resp:
			return resp.read()
	except HTTPError as exc:
		raise RuntimeError(f"GET {url} -> HTTP {exc.code}: {exc.reason}") from exc
	except URLError as exc:
		raise RuntimeError(f"GET {url} -> network error: {exc.reason}") from exc


# ---------------------------------------------------------------------------- #
# Pure detection logic.
# ---------------------------------------------------------------------------- #
def is_superseded_by_newer_base_run(
	run: HeartbeatRun,
	runs: Sequence[HeartbeatRun] | tuple[HeartbeatRun, ...],
) -> bool:
	"""True if a strictly newer non-coalesced base run exists in the window (FLO-419).

	Under ``coalesce_if_active`` concurrency the platform only admits a fresh
	base run when it considers the prior base run inactive. So the existence of
	a strictly newer base run (a run whose ``coalesced_into_run_id`` is null and
	whose ``triggered_at_epoch`` is greater) is proof the agent has moved on:
	the older in-flight run-record is a **zombie** (stale), not a stuck run.

	Used by :func:`detect_silent_runs` to suppress false silent-run alerts, and
	exposed publicly so :func:`tools.ops.agent_liveness.recovery.classify_run`
	can reach the same verdict without re-deriving the rule (DRY — one owner for
	the supersession test, no coupling to issue-status semantics).
	"""
	runs_t = runs if isinstance(runs, tuple) else tuple(runs)
	for other in runs_t:
		if other.id == run.id:
			continue
		# A coalesced overlap is not a base run — it folds into a sibling and
		# says nothing about whether the agent moved past `run`.
		if other.coalesced_into_run_id:
			continue
		if other.triggered_at_epoch > run.triggered_at_epoch:
			return True
	return False


def detect_silent_runs(
	runs: Sequence[HeartbeatRun] | tuple[HeartbeatRun, ...],
	now_epoch: float,
	*,
	warning_minutes: float = DEFAULT_WARNING_SILENCE_MINUTES,
	critical_minutes: float = DEFAULT_CRITICAL_SILENCE_MINUTES,
) -> tuple[SilentRunAlert, ...]:
	"""Find in-flight runs whose age crossed a silence threshold (FLO-266).

	A run is a silent-run candidate when:

	* its ``status`` is in :data:`ACTIVE_RUN_STATUSES` (non-terminal), AND
	* it has no ``completed_at_epoch`` (still open), AND
	* it is not itself a coalesced overlap (``coalesced_into_run_id`` is null —
		the real active run it folded into is the one we want), AND
	* it is not a **zombie** superseded by a strictly newer non-coalesced base
		run (FLO-419: ``coalesce_if_active`` only admits a fresh base run once
		the prior one is inactive, so a newer base run means the agent moved on
		and the older in-flight record is stale, not stuck).

	Severity is :data:`SEVERITY_CRITICAL` once ``age >= critical_minutes``,
	otherwise :data:`SEVERITY_WARNING` once ``age >= warning_minutes``. Alerts are
	returned oldest-first (the longest-stuck run leads the dashboard).
	"""
	# Import-free alias to avoid a circular import at module scope.
	runs_t = runs if isinstance(runs, tuple) else tuple(runs)
	candidates: list[SilentRunAlert] = []
	for run in runs_t:
		if run.status not in ACTIVE_RUN_STATUSES:
			continue
		if run.completed_at_epoch is not None:
			continue
		if run.coalesced_into_run_id:
			continue
		if is_superseded_by_newer_base_run(run, runs_t):
			continue  # FLO-419: zombie run-record — agent admitted a newer base run.
		age = _age_minutes(run.triggered_at_epoch, now_epoch)
		if age is None:
			continue
		if age >= critical_minutes:
			candidates.append(SilentRunAlert(run=run, age_minutes=age, severity=SEVERITY_CRITICAL))
		elif age >= warning_minutes:
			candidates.append(SilentRunAlert(run=run, age_minutes=age, severity=SEVERITY_WARNING))
	candidates.sort(key=lambda a: a.age_minutes, reverse=True)
	return tuple(candidates)


def _active_in_flight_runs(
	runs: tuple[HeartbeatRun, ...],
) -> tuple[HeartbeatRun, ...]:
	"""All non-coalesced in-flight runs (the 'what is the CEO doing now' set)."""
	return tuple(
		r
		for r in runs
		if r.status in ACTIVE_RUN_STATUSES and r.completed_at_epoch is None and not r.coalesced_into_run_id
	)


def compute_completion_stats(
	runs: tuple[HeartbeatRun, ...],
	*,
	now_epoch: float | None = None,
) -> CompletionStats:
	"""Duration trends over the *completed* runs in the window (FLO-266).

	A completed run contributes ``completed_at - triggered_at`` minutes. Runs
	that failed still contribute their duration (a slow failure is as much a
	latency signal as a slow success); coalesced overlaps are skipped because
	they carry no duration of their own.

	Percentiles use nearest-rank interpolation (simple, exact for small windows,
	and matches the dashboard's "how long does a CEO heartbeat take" question).
	"""
	durations: list[float] = []
	failure_count = 0
	for r in runs:
		if r.completed_at_epoch is None or r.triggered_at_epoch is None:
			continue
		if r.status == "coalesced":
			continue
		duration = (r.completed_at_epoch - r.triggered_at_epoch) / 60.0
		if duration < 0:
			continue
		durations.append(duration)
		if r.status == "failed":
			failure_count += 1
	if not durations:
		return CompletionStats(
			count=0,
			failure_count=failure_count,
			p50_minutes=None,
			p95_minutes=None,
			max_minutes=None,
			mean_minutes=None,
			durations_minutes=(),
		)
	durations.sort()
	count = len(durations)
	mean = sum(durations) / count

	def _percentile(q: float) -> float:
		# Nearest-rank: clamp the rank to [1, count].
		rank = max(1, min(count, math.ceil(q * count)))
		return durations[rank - 1]

	return CompletionStats(
		count=count,
		failure_count=failure_count,
		p50_minutes=_percentile(0.5),
		p95_minutes=_percentile(0.95),
		max_minutes=durations[-1],
		mean_minutes=mean,
		durations_minutes=tuple(durations),
	)


def _liveness_severity(
	agent: CEOAgentState,
	now_epoch: float,
	*,
	cadence_minutes: float = DEFAULT_HEARTBEAT_CADENCE_MINUTES,
	warning_minutes: float = DEFAULT_WARNING_SILENCE_MINUTES,
	critical_minutes: float = DEFAULT_CRITICAL_SILENCE_MINUTES,
) -> str:
	"""Derive a liveness severity from ``lastHeartbeatAt`` + org-chain health.

	The CEO heartbeat cadence is 15 minutes; if the agent has not heartbeated
	within the warning/critical silence budgets, the liveness signal itself is
	stale (independent of any individual run going silent).
	"""
	if agent.org_chain_health_status == "unhealthy":
		return SEVERITY_CRITICAL
	last = agent.last_heartbeat_at_epoch
	if last is not None:
		staleness = _age_minutes(last, now_epoch)
		if staleness is not None:
			if staleness >= critical_minutes:
				return SEVERITY_CRITICAL
			if staleness >= warning_minutes:
				return SEVERITY_WARNING
	return SEVERITY_OK


def _max_severity(*levels: str) -> str:
	"""Reduce a set of severities to the worst."""
	if SEVERITY_CRITICAL in levels:
		return SEVERITY_CRITICAL
	if SEVERITY_WARNING in levels:
		return SEVERITY_WARNING
	return SEVERITY_OK


def severity_to_exit_code(severity: str) -> int:
	"""Map a snapshot severity to a process exit code (UNIX monitoring convention)."""
	if severity == SEVERITY_CRITICAL:
		return EXIT_CRITICAL
	if severity == SEVERITY_WARNING:
		return EXIT_WARNING
	return EXIT_OK


# ---------------------------------------------------------------------------- #
# The monitor: one scrape -> one CEOHealthSnapshot.
# ---------------------------------------------------------------------------- #
@dataclass
class CEOHeartbeatMonitor:
	"""Compose a :class:`CEOHeartbeatSource` with the detection thresholds.

	Production wiring (the CLI) injects :class:`PaperclipCEOHeartbeatSource`;
	the unit suite injects :class:`StaticCEOHeartbeatSource`. ``evaluate_health``
	is the single dashboard / scrape entry point and is pure given a source read
	plus a clock — so the detection math is fully testable without a network.
	"""

	source: CEOHeartbeatSource
	warning_minutes: float = DEFAULT_WARNING_SILENCE_MINUTES
	critical_minutes: float = DEFAULT_CRITICAL_SILENCE_MINUTES
	cadence_minutes: float = DEFAULT_HEARTBEAT_CADENCE_MINUTES
	run_limit: int = 50
	clock: Callable[[], float] | None = None

	def _now(self) -> float:
		# Lazy typing for the clock callable keeps the module import-clean.
		return (self.clock or time.time)()

	def evaluate_health(self) -> CEOHealthSnapshot:
		"""Read the source once and return a :class:`CEOHealthSnapshot`."""
		now_epoch = self._now()
		agent, runs = self.source.fetch(self.run_limit)
		silent = detect_silent_runs(
			runs,
			now_epoch,
			warning_minutes=self.warning_minutes,
			critical_minutes=self.critical_minutes,
		)
		stats = compute_completion_stats(runs, now_epoch=now_epoch)
		active = _active_in_flight_runs(runs)
		# The "current operation" is the most-recently-triggered active run; if
		# none, the most-recent run overall (what the CEO just finished).
		current: HeartbeatRun | None = None
		if active:
			current = max(active, key=lambda r: r.triggered_at_epoch)
		elif runs:
			current = max(runs, key=lambda r: r.triggered_at_epoch)

		alert_levels = tuple(a.severity for a in silent)
		liveness_level = _liveness_severity(
			agent,
			now_epoch,
			cadence_minutes=self.cadence_minutes,
			warning_minutes=self.warning_minutes,
			critical_minutes=self.critical_minutes,
		)
		severity = _max_severity(*alert_levels, liveness_level)
		return CEOHealthSnapshot(
			taken_at_epoch=now_epoch,
			severity=severity,
			agent=agent,
			active_runs=active,
			silent_run_alerts=silent,
			completion_stats=stats,
			current_operation=current,
			runs_inspected=len(runs),
		)


def snapshot_as_text(snap: CEOHealthSnapshot) -> str:
	"""Human-readable multi-line report (the watchdog's stdout body)."""
	from datetime import datetime

	def _fmt(epoch: float | None) -> str:
		if epoch is None:
			return "unknown"
		return datetime.fromtimestamp(epoch, tz=UTC).strftime("%Y-%m-%d %H:%M:%SZ")

	lines: list[str] = []
	lines.append(f"CEO heartbeat health: {snap.severity.upper()}")
	lines.append(
		f"  agent: {snap.agent.name} status={snap.agent.status} "
		f"last_heartbeat={_fmt(snap.agent.last_heartbeat_at_epoch)} "
		f"chain={snap.agent.org_chain_health_status or 'unknown'}"
	)
	stats = snap.completion_stats
	if stats.count:
		lines.append(
			f"  completion (n={stats.count}, failures={stats.failure_count}): "
			f"p50={_fmt_dur(stats.p50_minutes)} p95={_fmt_dur(stats.p95_minutes)} "
			f"max={_fmt_dur(stats.max_minutes)} mean={_fmt_dur(stats.mean_minutes)}"
		)
	else:
		lines.append("  completion: no completed runs in window")
	if snap.silent_run_alerts:
		lines.append(f"  SILENT RUNS ({len(snap.silent_run_alerts)}):")
		for a in snap.silent_run_alerts:
			r = a.run
			what = r.linked_issue_identifier or r.linked_issue_title or r.id
			lines.append(
				f"    [{a.severity.upper()}] run {r.id} age={a.age_minutes:.1f}m "
				f"status={r.status} on {what} (started {_fmt(r.triggered_at_epoch)})"
			)
	elif snap.active_runs:
		lines.append(f"  active runs: {len(snap.active_runs)} (within threshold)")
	else:
		lines.append("  active runs: none")
	cur = snap.current_operation
	if cur:
		what = cur.linked_issue_identifier or cur.linked_issue_title or cur.id
		lines.append(f"  current operation: {what} [{cur.linked_issue_status or cur.status}]")
	lines.append(f"  runs inspected: {snap.runs_inspected}")
	return "\n".join(lines)


def _fmt_dur(minutes: float | None) -> str:
	if minutes is None:
		return "n/a"
	if minutes < 1.0:
		return f"{minutes * 60:.0f}s"
	return f"{minutes:.1f}m"


def snapshot_as_json(snap: CEOHealthSnapshot) -> str:
	"""Machine-readable JSON report (structured alerting / log shipping)."""
	from datetime import datetime

	def _iso(epoch: float | None) -> str | None:
		if epoch is None:
			return None
		return datetime.fromtimestamp(epoch, tz=UTC).isoformat().replace("+00:00", "Z")

	stats = snap.completion_stats
	cur = snap.current_operation
	payload = {
		"severity": snap.severity,
		"taken_at": _iso(snap.taken_at_epoch),
		"agent": {
			"id": snap.agent.id,
			"name": snap.agent.name,
			"status": snap.agent.status,
			"last_heartbeat_at": _iso(snap.agent.last_heartbeat_at_epoch),
			"updated_at": _iso(snap.agent.updated_at_epoch),
			"org_chain_health": snap.agent.org_chain_health_status,
			"org_chain_health_reason": snap.agent.org_chain_health_reason,
		},
		"completion": {
			"count": stats.count,
			"failure_count": stats.failure_count,
			"p50_minutes": stats.p50_minutes,
			"p95_minutes": stats.p95_minutes,
			"max_minutes": stats.max_minutes,
			"mean_minutes": stats.mean_minutes,
		},
		"active_runs": [
			{
				"id": r.id,
				"status": r.status,
				"triggered_at": _iso(r.triggered_at_epoch),
				"linked_issue": r.linked_issue_identifier,
				"linked_issue_status": r.linked_issue_status,
			}
			for r in snap.active_runs
		],
		"silent_run_alerts": [
			{
				"severity": a.severity,
				"run_id": a.run.id,
				"age_minutes": round(a.age_minutes, 1),
				"status": a.run.status,
				"triggered_at": _iso(a.run.triggered_at_epoch),
				"linked_issue": a.run.linked_issue_identifier,
				"linked_issue_id": a.run.linked_issue_id,
				"linked_issue_status": a.run.linked_issue_status,
				"failure_reason": a.run.failure_reason,
			}
			for a in snap.silent_run_alerts
		],
		"current_operation": (
			{
				"run_id": cur.id,
				"linked_issue": cur.linked_issue_identifier,
				"linked_issue_id": cur.linked_issue_id,
				"linked_issue_title": cur.linked_issue_title,
				"linked_issue_status": cur.linked_issue_status,
				"status": cur.status,
			}
			if cur
			else None
		),
		"runs_inspected": snap.runs_inspected,
	}
	return json.dumps(payload, indent=2, sort_keys=True)


def snapshot_as_prometheus(snap: CEOHealthSnapshot) -> str:
	"""Prometheus text exposition (FLO-266 dashboard scrape surface).

	Mirrors :meth:`flock_os.telemetry.TelemetrySnapshot.as_prometheus` so a
	single scrape job can feed both the runtime + the org-health dashboard. One
	gauge per signal + a ``ceo_silent_run`` series per alert so the alertmanager
	routing keys off the label set.
	"""
	lines: list[str] = [
		"# HELP ceo_health_severity CEO heartbeat health severity (0=ok,1=warning,2=critical).",
		"# TYPE ceo_health_severity gauge",
		f"ceo_health_severity {severity_to_exit_code(snap.severity)}",
		"# HELP ceo_last_heartbeat_timestamp_seconds Unix epoch of the CEO's last heartbeat.",
		"# TYPE ceo_last_heartbeat_timestamp_seconds gauge",
		f"ceo_last_heartbeat_timestamp_seconds {snap.agent.last_heartbeat_at_epoch or 0}",
		"# HELP ceo_active_run_count In-flight CEO heartbeat runs right now.",
		"# TYPE ceo_active_run_count gauge",
		f"ceo_active_run_count {len(snap.active_runs)}",
		"# HELP ceo_silent_run_alert_count Silent CEO runs currently over threshold.",
		"# TYPE ceo_silent_run_alert_count gauge",
		f"ceo_silent_run_alert_count {len(snap.silent_run_alerts)}",
	]
	stats = snap.completion_stats
	if stats.count:
		lines += [
			"# HELP ceo_heartbeat_completion_seconds CEO heartbeat run duration (completed runs).",
			"# TYPE ceo_heartbeat_completion_seconds summary",
			f'ceo_heartbeat_completion_seconds{{quantile="0.5"}} {(stats.p50_minutes or 0) * 60}',
			f'ceo_heartbeat_completion_seconds{{quantile="0.95"}} {(stats.p95_minutes or 0) * 60}',
			f'ceo_heartbeat_completion_seconds{{quantile="max"}} {(stats.max_minutes or 0) * 60}',
			f"ceo_heartbeat_completion_seconds_count {stats.count}",
			f"ceo_heartbeat_completion_seconds_sum {sum(stats.durations_minutes) * 60}",
		]
	for a in snap.silent_run_alerts:
		labels = (
			f'severity="{a.severity}",run_id="{a.run.id}",'
			f'linked_issue="{a.run.linked_issue_identifier or ""}"'
		)
		lines += [
			"# HELP ceo_silent_run_age_minutes Age (minutes) of a silent CEO run.",
			"# TYPE ceo_silent_run_age_minutes gauge",
			f"ceo_silent_run_age_minutes{{{labels}}} {a.age_minutes:.1f}",
		]
	return "\n".join(lines) + "\n"
