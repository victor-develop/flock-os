#!/usr/bin/env python3
"""
CEO heartbeat timeout watchdog (FLO-265).

WHAT
----
A defense-in-depth timeout enforcer for the CEO heartbeat. The CEO runs every
15 minutes (``company.heartbeat`` in ``.paperclip/manifest.json``); the
``heartbeatConfig.timeoutSeconds`` knob (default 720s / 12 min, within the
10–15 min target from the [FLO-264](/FLO/issues/FLO-264) silent-run analysis)
bounds how long a *single* CEO run may stay active. The Paperclip control
plane does not expose a native per-run timeout for the ``opencode_local``
adapter, so this watchdog supplies one: detect an over-time active run via
the API, terminate the stuck local ``opencode run`` process, and post an
incident comment so the org is never silently paralysed by a stuck CEO again.

WHY A WATCHDOG (not just config)
--------------------------------
The CEO is the linchpin of the org chart: every other agent reports (in)directly
to the CEO, so a stuck CEO is a single point of failure for the whole company.
``docs/architecture/ceo-silent-run-analysis.md`` records the 2026-06-20 incident
where a CEO run stayed silent for >1 hour with no timeout. The manifest knob is
the *declaration* of policy; this script is the *enforcement*. Together they
close recommendation #1 of that analysis.

DESIGN
------
* Pure, side-effect-free decision logic (manifest parse + ISO-8601 age math +
  active/timed-out predicates) is factored out so it is unit-tested by
  ``flock_os/tests/test_ceo_heartbeat_watchdog.py`` and exercised by
  ``--self-test``. The CLI only adds HTTP + process side effects.
* Stdlib only (``urllib`` + ``subprocess``) — no third-party deps, so it runs
  in the bare CI venv and on the operator Mac without ``pip install``.
* Idempotent and safe to cron every minute: re-runs re-evaluate from the API;
  a run that already ended is ignored; termination is best-effort and always
  paired with an incident comment.

USAGE
-----
    # One-shot check + enforce (the cron entry point):
    python3 scripts/dev/ceo-heartbeat-watchdog.py

    # Observe-only: report what *would* happen, kill nothing:
    python3 scripts/dev/ceo-heartbeat-watchdog.py --dry-run

    # Loop (foreground daemon, useful when not run from launchd/cron):
    python3 scripts/dev/ceo-heartbeat-watchdog.py --interval 60

    # Pure-logic self-test (no API, no processes) — also used by the runbook:
    python3 scripts/dev/ceo-heartbeat-watchdog.py --self-test

Env (all required for live enforcement, ignored by ``--self-test``):

    PAPERCLIP_API_URL   base API URL (e.g. http://127.0.0.1:... )
    PAPERCLIP_API_KEY   bearer token (run JWT or long-lived agent key)
    PAPERCLIP_COMPANY_ID  company id hosting the CEO agent + routine

Optional:

    FLOCK_CEO_AGENT_ID       CEO agent id (else resolved via /api/.../agents by role)
    FLOCK_CEO_TERMINATE_CMD  custom kill command; ``{pid}`` is substituted.
                             Overrides the built-in opencode-process matcher —
                             wire this for adapter-specific kill paths.

See ``docs/operations/ceo-heartbeat-timeout.md`` for the full runbook.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime

# --- Policy defaults -------------------------------------------------------- #
# 12 minutes: inside the 10–15 min target, above the 5–10 min expected run
# length, and below the 15-min heartbeat cadence so a killed run frees the
# slot before the next cycle fires (which would otherwise coalesce into it).
DEFAULT_TIMEOUT_SECONDS = 720
DEFAULT_GRACE_SECONDS = 60
# Sane validation bounds — refuse to silently enforce an absurd manifest value.
MIN_TIMEOUT_SECONDS = 60
MAX_TIMEOUT_SECONDS = 3600
# Routine-run statuses that mean the run is still in flight (no completedAt).
ACTIVE_RUN_STATUSES = frozenset({"running", "active", "dispatched", "pending", "queued", "executing"})
# HeartbeatConfig keys recognised on disk.
CFG_TIMEOUT = "timeoutSeconds"
CFG_GRACE = "graceSeconds"
CFG_AGENT = "agent"


# =========================================================================== #
# Pure decision logic (unit-tested; no I/O, no clock unless injected)
# =========================================================================== #
def load_manifest(path: str) -> dict:
	"""Load and JSON-parse the manifest at ``path``."""
	with open(path, encoding="utf-8") as fh:
		return json.load(fh)


def heartbeat_config(manifest: dict) -> dict:
	"""Return the ``company.heartbeatConfig`` object (empty dict if absent)."""
	company = manifest.get("company") or {}
	cfg = company.get("heartbeatConfig")
	if isinstance(cfg, dict):
		return cfg
	return {}


def coerce_timeout_seconds(value, context: str) -> int:
	"""Validate a timeout scalar; raise ``ValueError`` with a useful message.

	Accepts only a true ``int`` (booleans, floats, and numeric strings are
	rejected — a manifest author who meant ``720`` should write ``720``, and a
	quoted/mistyped value almost always indicates a misconfiguration we want
	to surface rather than silently coerce).
	"""
	if isinstance(value, bool):
		raise ValueError(f"{context} must be a positive integer, got boolean")
	if not isinstance(value, int):
		raise ValueError(f"{context} must be an integer, got {type(value).__name__} {value!r}")
	seconds = value
	if seconds < MIN_TIMEOUT_SECONDS or seconds > MAX_TIMEOUT_SECONDS:
		raise ValueError(
			f"{context}={seconds}s out of bounds "
			f"[{MIN_TIMEOUT_SECONDS}, {MAX_TIMEOUT_SECONDS}] "
			f"(target 10–15 min; default {DEFAULT_TIMEOUT_SECONDS}s)"
		)
	return seconds


def heartbeat_timeout_seconds(manifest: dict, default: int = DEFAULT_TIMEOUT_SECONDS) -> int:
	"""Resolve the CEO heartbeat timeout (seconds) from the manifest.

	Reads ``company.heartbeatConfig.timeoutSeconds``; falls back to ``default``
	when the key is entirely absent. An out-of-bounds or wrong-typed value is a
	policy error, so it raises ``ValueError`` rather than silently coercing.
	"""
	cfg = heartbeat_config(manifest)
	if CFG_TIMEOUT in cfg:
		return coerce_timeout_seconds(cfg[CFG_TIMEOUT], "heartbeatConfig.timeoutSeconds")
	# Legacy/spelling-tolerant fallbacks (no enforcement if absent).
	company = manifest.get("company") or {}
	for alt in ("heartbeatTimeoutSeconds", "heartbeat_timeout_seconds"):
		if alt in company:
			return coerce_timeout_seconds(company[alt], f"company.{alt}")
	return coerce_timeout_seconds(default, "default")


def heartbeat_grace_seconds(manifest: dict, default: int = DEFAULT_GRACE_SECONDS) -> int:
	"""Resolve the SIGTERM→SIGKILL grace period (seconds)."""
	cfg = heartbeat_config(manifest)
	raw = cfg.get(CFG_GRACE, default)
	seconds = int(raw)
	if seconds < 0:
		raise ValueError(f"heartbeatConfig.{CFG_GRACE} must be >= 0, got {seconds}")
	return seconds


def heartbeat_agent_role(manifest: dict, default: str = "ceo") -> str:
	"""Resolve the role the heartbeat is attached to (default ``ceo``)."""
	cfg = heartbeat_config(manifest)
	role = cfg.get(CFG_AGENT, default)
	if not isinstance(role, str) or not role.strip():
		raise ValueError(f"heartbeatConfig.{CFG_AGENT} must be a non-empty string")
	return role


def parse_iso8601_utc(value) -> float | None:
	"""Parse an ISO-8601 timestamp (e.g. ``2026-06-20T02:15:20.275Z``) to epoch
	seconds. Returns ``None`` for empty/invalid input. Naive timestamps are
	assumed UTC.
	"""
	if not value or not isinstance(value, str):
		return None
	text = value.strip()
	if not text:
		return None
	# ``datetime.fromisoformat`` (3.11+) accepts a trailing ``Z``.
	text = text.replace("Z", "+00:00")
	try:
		dt = datetime.fromisoformat(text)
	except ValueError:
		return None
	if dt.tzinfo is None:
		dt = dt.replace(tzinfo=UTC)
	return dt.timestamp()


def run_age_seconds(run: dict, now_epoch: float) -> float | None:
	"""Age of a run in seconds, measured from ``triggeredAt``. ``None`` if the
	run has no usable start timestamp.
	"""
	started = parse_iso8601_utc(run.get("triggeredAt") or run.get("startedAt"))
	if started is None:
		return None
	return max(0.0, now_epoch - started)


# Routine-run statuses that are terminal — the routine fired and handed off
# (e.g. ``issue_created`` = enqueued an issue; the CEO then executes in a
# *separate* agent run tracked by an environment lease, not by this routine
# run). Treating these as active would false-positive on every heartbeat.
ROUTINE_TERMINAL_STATUSES = frozenset(
	{
		"coalesced",
		"completed",
		"succeeded",
		"failed",
		"cancelled",
		"error",
		"done",
		"issue_created",
	}
)


def is_run_active(run: dict) -> bool:
	"""True iff the run is still in flight (no ``completedAt`` and a status that
	is not terminal). Unknown statuses are treated as active when
	``completedAt`` is absent — fail-open toward detection so a novel adapter
	status can never hide a stuck run.
	"""
	if run.get("completedAt"):
		return False
	status = (run.get("status") or "").lower()
	if not status:
		return True  # no status + no completedAt => assume active (fail-open)
	return status not in ROUTINE_TERMINAL_STATUSES


def is_run_timed_out(run: dict, now_epoch: float, timeout_seconds: int) -> bool:
	"""True iff ``run`` is active and its age has reached the timeout."""
	if not is_run_active(run):
		return False
	age = run_age_seconds(run, now_epoch)
	if age is None:
		return False
	return age >= timeout_seconds


def select_timed_out_runs(runs: list, now_epoch: float, timeout_seconds: int) -> list:
	"""Return the subset of ``runs`` that are active and over the timeout."""
	return [r for r in runs if is_run_timed_out(r, now_epoch, timeout_seconds)]


# --- Open execution-lease detection (primary "CEO is executing" signal) ----- #
# A CEO heartbeat execution is bracketed in the company activity log by
# ``environment.lease_acquired`` ... ``environment.lease_released`` events that
# share a ``runId``. An *open* lease (acquired, never released) whose age passed
# the timeout is a genuinely stuck CEO execution — this is exactly the shape of
# the FLO-264 silent run (lease held 02:15 -> 03:33). Routine-run statuses are
# NOT used for this: by the time the CEO is executing, the routine run has
# already gone terminal (``issue_created``).
LEASE_ACQUIRED = "environment.lease_acquired"
LEASE_RELEASED = "environment.lease_released"


def open_leases_from_activity(
	events: list,
	agent_id: str,
	now_epoch: float,
	lookback_seconds: float = 3600.0,
) -> list:
	"""Return open execution leases for ``agent_id`` from raw activity events.

	Each returned item is ``{run_id, acquired_at_epoch, issue_id, age_seconds}``
	for a run whose most recent lease event (within ``lookback_seconds``) is an
	acquire with no matching release — i.e. the agent is still executing it.
	Pure and clock-injected (``now_epoch``) so it is unit-testable. Events for
	other agents and non-lease actions are ignored.
	"""
	horizon = now_epoch - lookback_seconds
	latest_by_run: dict[str, dict] = {}
	acquired_at: dict[str, float] = {}
	issue_by_run: dict[str, str | None] = {}
	for ev in events or []:
		if (ev.get("agentId") or "") != agent_id:
			continue
		action = ev.get("action")
		if action not in (LEASE_ACQUIRED, LEASE_RELEASED):
			continue
		run_id = ev.get("runId")
		if not run_id:
			continue
		created = parse_iso8601_utc(ev.get("createdAt"))
		if created is None or created < horizon:
			continue
		# Keep the chronologically latest event per runId.
		prior = latest_by_run.get(run_id)
		if prior is None or (parse_iso8601_utc(prior.get("createdAt")) or 0.0) < created:
			latest_by_run[run_id] = ev
			if action == LEASE_ACQUIRED:
				acquired_at[run_id] = created
				details = ev.get("details") or {}
				issue_by_run[run_id] = details.get("issueId")
	open_runs = []
	for run_id, ev in latest_by_run.items():
		if ev.get("action") != LEASE_ACQUIRED:
			continue
		acq = acquired_at.get(run_id)
		if acq is None:
			continue
		open_runs.append(
			{
				"run_id": run_id,
				"acquired_at_epoch": acq,
				"issue_id": issue_by_run.get(run_id),
				"age_seconds": max(0.0, now_epoch - acq),
			}
		)
	open_runs.sort(key=lambda item: item["age_seconds"], reverse=True)
	return open_runs


# =========================================================================== #
# Live enforcement layer (HTTP + process side effects)
# =========================================================================== #
class Watchdog:
	"""Thin client that adds the API + process side effects around the pure
	decision logic. Constructed with env-derived config so it is easy to unit
	test indirectly via dependency injection.
	"""

	def __init__(
		self,
		api_url: str,
		api_token: str,
		company_id: str,
		agent_id: str | None = None,
		terminate_cmd: str | None = None,
		dry_run: bool = False,
	):
		self.api_url = api_url.rstrip("/")
		self.api_token = api_token
		self.company_id = company_id
		self.agent_id = agent_id
		self.terminate_cmd = terminate_cmd
		self.dry_run = dry_run

	# -- HTTP ---------------------------------------------------------------- #
	def _request(self, method: str, path: str, body: dict | None = None) -> object:
		url = f"{self.api_url}{path}"
		data = json.dumps(body).encode("utf-8") if body is not None else None
		req = urllib.request.Request(
			url,
			data=data,
			method=method,
			headers={
				"Authorization": f"Bearer {self.api_token}",
				"Content-Type": "application/json",
				"Accept": "application/json",
			},
		)
		try:
			with urllib.request.urlopen(req, timeout=15) as resp:
				raw = resp.read()
		except urllib.error.HTTPError as exc:
			raise RuntimeError(f"{method} {path} -> HTTP {exc.code}") from exc
		if not raw:
			return None
		try:
			return json.loads(raw)
		except json.JSONDecodeError:
			return None

	def resolve_agent_id(self, role: str) -> str:
		"""Resolve the agent id for ``role`` via the company agents list."""
		if self.agent_id:
			return self.agent_id
		agents = self._request("GET", f"/api/companies/{self.company_id}/agents")
		if not isinstance(agents, list):
			raise RuntimeError("agents list not returned from API")
		for agent in agents:
			if (agent.get("role") or "").lower() == role.lower():
				aid = agent.get("id")
				if aid:
					return aid
		raise RuntimeError(f"no agent with role {role!r} found")

	def list_open_executions(self, agent_id: str, now_epoch: float) -> list:
		"""Return the CEO's open execution leases (active heartbeat runs) from
		the company activity log. See ``open_leases_from_activity`` for the
		detection model.
		"""
		events = self._request("GET", f"/api/companies/{self.company_id}/activity?limit=100")
		if not isinstance(events, list):
			return []
		return open_leases_from_activity(events, agent_id, now_epoch)

	def post_incident_comment(
		self, issue_id: str, run_id: str, age_seconds: float, timeout_seconds: int, killed: bool
	) -> None:
		"""Post an incident comment. Observe-only in dry-run (no mutation)."""
		if self.dry_run:
			print(
				f"[dry-run] would post incident comment on issue {issue_id}: "
				f"run {run_id} over {int(age_seconds)}s (timeout {timeout_seconds}s)"
			)
			return
		action = "terminated" if killed else "failed to terminate"
		body = {
			"comment": (
				f"⏱️ CEO heartbeat timeout watchdog ({timeout_seconds}s) — "
				f"run `{run_id}` reached ~{int(age_seconds)}s active.\n\n"
				f"- Action: **{action}**\n"
				f"- Policy: `.paperclip/manifest.json` → `company.heartbeatConfig.timeoutSeconds`\n"
				f"- Runbook: `docs/operations/ceo-heartbeat-timeout.md`\n"
				f"- Source: [FLO-265](/FLO/issues/FLO-265)"
			)
		}
		try:
			self._request("POST", f"/api/issues/{issue_id}/comments", body)
		except RuntimeError as exc:
			# A comment failure must never mask the kill itself.
			print(f"warn: could not post incident comment: {exc}", file=sys.stderr)

	# -- Process termination ------------------------------------------------- #
	def terminate_run_process(self, lease: dict, agent_id: str, grace_seconds: int) -> bool:
		"""Best-effort terminate the stuck local opencode run.

		Returns True if a process was (or would be, in dry-run) terminated.
		If ``FLOCK_CEO_TERMINATE_CMD`` is set it is used (adapter-specific kill
		path); otherwise the oldest ``opencode run`` process at least as old as
		the over-time run is SIGTERM'd, then SIGKILL'd after the grace period.
		``lease`` is an open-lease record (``run_id`` + ``age_seconds``).
		"""
		run_id = lease.get("run_id", "")
		if self.terminate_cmd:
			return self._terminate_via_cmd(self.terminate_cmd, run_id, agent_id, grace_seconds)
		return self._terminate_opencode_process(lease, grace_seconds)

	def _terminate_via_cmd(self, cmd: str, run_id: str, agent_id: str, grace_seconds: int) -> bool:
		# Resolve a pid placeholder lazily; commands that don't use {pid} are
		# honoured verbatim (e.g. an HTTP-based terminate endpoint).
		rendered = (
			cmd.replace("{agent}", agent_id).replace("{run}", run_id).replace("{grace}", str(grace_seconds))
		)
		if self.dry_run:
			print(f"[dry-run] would run terminate cmd: {rendered}")
			return True
		print(f"terminate cmd: {rendered}")
		try:
			subprocess.run(rendered, shell=True, check=False, timeout=30)
			return True
		except subprocess.TimeoutExpired:
			print("warn: terminate command timed out", file=sys.stderr)
			return False

	def _terminate_opencode_process(self, lease: dict, grace_seconds: int) -> bool:
		"""Match and kill the oldest ``opencode run`` process at least as old as
		the over-time lease. Conservative: only acts when an over-time process
		is found, and never kills more than one per invocation.
		"""
		try:
			ps = subprocess.run(
				["ps", "-o", "pid=,etime=,command="],
				capture_output=True,
				text=True,
				check=True,
				timeout=10,
			)
		except (subprocess.SubprocessError, FileNotFoundError) as exc:
			print(f"warn: ps unavailable, cannot match process: {exc}", file=sys.stderr)
			return False
		age = lease.get("age_seconds")
		if age is None:
			return False
		# ps etime formats vary by host; match ``opencode run`` lines whose
		# elapsed time (parsed from the etime column) >= the run age. We keep
		# the OLDEST matching candidate to target the genuinely stuck run.
		candidates: list[tuple[int, float]] = []
		for line in ps.stdout.splitlines():
			stripped = line.strip()
			if "opencode" not in stripped or " run " not in stripped:
				continue
			parts = stripped.split(None, 2)
			if len(parts) < 3:
				continue
			try:
				pid = int(parts[0])
			except ValueError:
				continue
			elapsed = _parse_etime_seconds(parts[1])
			if elapsed is None:
				continue
			if elapsed >= age:
				candidates.append((pid, elapsed))
		if not candidates:
			return False
		candidates.sort(key=lambda item: item[1], reverse=True)
		pid, elapsed = candidates[0]
		return self._kill_pid(pid, elapsed, grace_seconds)

	def _kill_pid(self, pid: int, elapsed: float, grace_seconds: int) -> bool:
		if self.dry_run:
			print(f"[dry-run] would SIGTERM pid {pid} (elapsed {int(elapsed)}s)")
			return True
		print(f"SIGTERM pid {pid} (elapsed {int(elapsed)}s)")
		try:
			os.kill(pid, 15)  # SIGTERM
		except ProcessLookupError:
			return False
		except PermissionError as exc:
			print(f"warn: cannot signal pid {pid}: {exc}", file=sys.stderr)
			return False
		deadline = time.time() + max(0, grace_seconds)
		while time.time() < deadline:
			try:
				os.kill(pid, 0)  # existence probe
			except ProcessLookupError:
				return True
			except PermissionError:
				break
			time.sleep(1)
		print(f"SIGKILL pid {pid} after grace")
		try:
			os.kill(pid, 9)  # SIGKILL
		except ProcessLookupError:
			return True
		except PermissionError as exc:
			print(f"warn: cannot SIGKILL pid {pid}: {exc}", file=sys.stderr)
			return False
		return True

	# -- Orchestration ------------------------------------------------------- #
	def enforce(
		self,
		timeout_seconds: int,
		grace_seconds: int,
		role: str,
		now_epoch: float | None = None,
	) -> int:
		"""One enforcement pass. Returns the count of runs acted on.

		Detection uses open environment leases (the CEO's active execution
		window), not routine-run statuses — see ``open_leases_from_activity``.
		"""
		now = time.time() if now_epoch is None else now_epoch
		agent_id = self.resolve_agent_id(role)
		open_leases = self.list_open_executions(agent_id, now)
		over = [lease for lease in open_leases if lease.get("age_seconds", 0.0) >= timeout_seconds]
		if not over:
			print(f"ok: 0/{len(open_leases)} open CEO execution leases over {timeout_seconds}s timeout")
			return 0
		acted = 0
		for lease in over:
			age = lease.get("age_seconds", 0.0)
			issue_id = lease.get("issue_id")
			killed = self.terminate_run_process(lease, agent_id, grace_seconds)
			if issue_id:
				self.post_incident_comment(issue_id, lease.get("run_id", "?"), age, timeout_seconds, killed)
			acted += 1
		return acted


def _parse_etime_seconds(etime: str) -> float | None:
	"""Parse a POSIX ``ps`` elapsed-time token (``[[dd-]hh:]mm:ss``) to seconds.

	``ps`` on macOS uses ``mm:ss`` or ``hh:mm:ss``; Linux adds an optional
	``dd-`` day prefix. Returns ``None`` when the token does not parse.
	"""
	token = etime.strip()
	if not token:
		return None
	days = 0.0
	if "-" in token:
		day_part, _, token = token.partition("-")
		try:
			days = float(day_part)
		except ValueError:
			return None
	parts = token.split(":")
	if not parts or len(parts) > 3:
		return None
	try:
		nums = [float(p) for p in parts]
	except ValueError:
		return None
	secs = 0.0
	for num in nums:
		secs = secs * 60 + num
	return days * 86400 + secs


# =========================================================================== #
# Self-test (pure logic; no API, no processes) — used by the runbook gate
# =========================================================================== #
def self_test() -> int:
	"""Exercise the pure decision logic against fixed cases. Exit 0 on pass."""
	cases = []
	manifest = {
		"company": {
			"heartbeat": "CEO @ */15 * * * * (coalesce_if_active, skip_missed)",
			"heartbeatConfig": {
				"agent": "ceo",
				"cron": "*/15 * * * *",
				"timeoutSeconds": 720,
				"timeoutAction": "terminate",
				"graceSeconds": 60,
			},
		}
	}
	cases.append(("timeout from heartbeatConfig", heartbeat_timeout_seconds(manifest), 720))
	cases.append(("agent role", heartbeat_agent_role(manifest), "ceo"))
	cases.append(("grace seconds", heartbeat_grace_seconds(manifest), 60))

	empty = {"company": {}}
	cases.append(("default timeout when absent", heartbeat_timeout_seconds(empty), DEFAULT_TIMEOUT_SECONDS))

	# ISO-8601 parsing.
	started_epoch = parse_iso8601_utc("2026-06-20T02:15:20Z")
	cases.append(("parse Z timestamp", started_epoch is not None and started_epoch > 0, True))
	cases.append(("parse empty", parse_iso8601_utc(""), None))
	cases.append(("parse garbage", parse_iso8601_utc("not-a-date"), None))

	# Age + active predicates. Build ``now`` relative to the parsed start so the
	# test does not depend on a hand-computed epoch.
	now = (started_epoch or 0.0) + 1000.0  # 1000s after the run started
	run_active = {
		"id": "run-1",
		"status": "running",
		"triggeredAt": "2026-06-20T02:15:20Z",
		"completedAt": None,
	}
	run_done = {
		"id": "run-2",
		"status": "completed",
		"triggeredAt": "2026-06-20T02:15:20Z",
		"completedAt": "2026-06-20T02:25:00Z",
	}
	run_coalesced = {
		"id": "run-3",
		"status": "coalesced",
		"triggeredAt": "2026-06-20T02:15:20Z",
		"completedAt": "2026-06-20T02:15:20Z",
	}
	cases.append(("active running is active", is_run_active(run_active), True))
	cases.append(("completed is not active", is_run_active(run_done), False))
	cases.append(("coalesced is not active", is_run_active(run_coalesced), False))
	cases.append(("active run age", run_age_seconds(run_active, now), 1000.0))
	cases.append(
		("timed out under 720? no (age 1000 >= 720 yes)", is_run_timed_out(run_active, now, 720), True)
	)
	cases.append(
		("timed out under 2000? no (age 1000 < 2000)", is_run_timed_out(run_active, now, 2000), False)
	)
	cases.append(("done run never times out", is_run_timed_out(run_done, now, 720), False))
	cases.append(
		(
			"select filters to over-time only",
			len(select_timed_out_runs([run_active, run_done, run_coalesced], now, 720)),
			1,
		)
	)
	# issue_created routine status is terminal (no false positive on heartbeat).
	run_issue_created = {
		"status": "issue_created",
		"triggeredAt": "2026-06-20T02:15:20Z",
		"completedAt": None,
	}
	cases.append(("issue_created is not active", is_run_active(run_issue_created), False))

	# Open-lease detection from a synthetic activity stream.
	ceo = "ceo-agent-id"
	other_agent = "someone-else"
	activity = [
		{
			"agentId": ceo,
			"action": LEASE_ACQUIRED,
			"runId": "open-run",
			"createdAt": "2026-06-20T02:15:20Z",
			"details": {"issueId": "issue-open"},
		},
		{
			"agentId": ceo,
			"action": LEASE_ACQUIRED,
			"runId": "closed-run",
			"createdAt": "2026-06-20T01:00:00Z",
			"details": {"issueId": "issue-closed"},
		},
		{
			"agentId": ceo,
			"action": LEASE_RELEASED,
			"runId": "closed-run",
			"createdAt": "2026-06-20T01:05:00Z",
		},
		{
			"agentId": other_agent,
			"action": LEASE_ACQUIRED,
			"runId": "other-run",
			"createdAt": "2026-06-20T02:00:00Z",
		},
		{
			"agentId": ceo,
			"action": "issue.comment_added",
			"runId": "noisy-run",
			"createdAt": "2026-06-20T02:30:00Z",
		},
	]
	open_leases = open_leases_from_activity(activity, ceo, now, lookback_seconds=7200)
	cases.append(("one open lease for ceo", len(open_leases), 1))
	cases.append(("open lease id", open_leases[0]["run_id"], "open-run"))
	cases.append(("open lease issue", open_leases[0]["issue_id"], "issue-open"))
	cases.append(("open lease age", int(open_leases[0]["age_seconds"]), 1000))
	cases.append(
		(
			"open lease lookback filters ancient events",
			len(open_leases_from_activity(activity, ceo, now, lookback_seconds=60)),
			0,
		)
	)

	# etime parsing (macOS + Linux shapes).
	cases.append(("etime mm:ss", _parse_etime_seconds("12:00"), 720.0))
	cases.append(("etime hh:mm:ss", _parse_etime_seconds("01:02:03"), 3723.0))
	cases.append(("etime dd-... ", _parse_etime_seconds("1-00:00:00"), 86400.0))
	cases.append(("etime garbage", _parse_etime_seconds("n/a"), None))

	# Validation rejects absurd policy.
	_bad = {"company": {"heartbeatConfig": {"timeoutSeconds": 5}}}
	try:
		heartbeat_timeout_seconds(_bad)
		cases.append(("reject sub-min timeout", "raised", "raised"))
	except ValueError:
		cases.append(("reject sub-min timeout", "raised", "raised"))

	failures = 0
	for name, got, want in cases:
		ok = got == want
		# bool/int equivalence guard: True == 1 in Python — flag accidental types.
		if isinstance(want, bool) != isinstance(got, bool):
			ok = False
		if ok:
			print(f"  PASS  {name}")
		else:
			failures += 1
			print(f"  FAIL  {name}: got {got!r}, want {want!r}")
	print(f"self-test: {len(cases) - failures}/{len(cases)} passed")
	return 1 if failures else 0


# =========================================================================== #
# CLI
# =========================================================================== #
def build_arg_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(description="CEO heartbeat timeout watchdog (FLO-265).")
	parser.add_argument(
		"--manifest",
		default=None,
		help="path to .paperclip/manifest.json (default: auto-detect from this script location)",
	)
	parser.add_argument("--once", action="store_true", help="run a single enforcement pass (default)")
	parser.add_argument(
		"--interval",
		type=int,
		default=None,
		help="loop forever, sleeping this many seconds between passes",
	)
	parser.add_argument(
		"--dry-run",
		action="store_true",
		help="observe-only: report what would happen, terminate nothing",
	)
	parser.add_argument(
		"--self-test",
		action="store_true",
		help="run pure-logic self-test (no API, no processes) and exit",
	)
	return parser


def default_manifest_path() -> str:
	here = os.path.dirname(os.path.abspath(__file__))
	# scripts/dev/ -> repo root -> .paperclip/manifest.json
	root = os.path.abspath(os.path.join(here, "..", ".."))
	return os.path.join(root, ".paperclip", "manifest.json")


def main(argv: list[str] | None = None) -> int:
	args = build_arg_parser().parse_args(argv)

	if args.self_test:
		return self_test()

	manifest_path = args.manifest or default_manifest_path()
	try:
		manifest = load_manifest(manifest_path)
	except FileNotFoundError:
		print(f"error: manifest not found at {manifest_path}", file=sys.stderr)
		return 2
	timeout_seconds = heartbeat_timeout_seconds(manifest)
	grace_seconds = heartbeat_grace_seconds(manifest)
	role = heartbeat_agent_role(manifest)
	print(
		f"watchdog: role={role} timeout={timeout_seconds}s grace={grace_seconds}s "
		f"manifest={manifest_path} dry_run={args.dry_run}"
	)

	if not args.self_test:
		# Live enforcement needs API creds.
		api_url = os.environ.get("PAPERCLIP_API_URL")
		api_token = os.environ.get("PAPERCLIP_API_KEY")
		company_id = os.environ.get("PAPERCLIP_COMPANY_ID")
		if not (api_url and api_token and company_id):
			print(
				"error: PAPERCLIP_API_URL, PAPERCLIP_API_KEY, and "
				"PAPERCLIP_COMPANY_ID are required for live enforcement "
				"(use --self-test for the pure-logic check).",
				file=sys.stderr,
			)
			return 2
		wd = Watchdog(
			api_url=api_url,
			api_token=api_token,
			company_id=company_id,
			agent_id=os.environ.get("FLOCK_CEO_AGENT_ID"),
			terminate_cmd=os.environ.get("FLOCK_CEO_TERMINATE_CMD"),
			dry_run=args.dry_run,
		)

		def _pass() -> int:
			return wd.enforce(timeout_seconds, grace_seconds, role)

		if args.interval:
			while True:
				try:
					_pass()
				except RuntimeError as exc:
					print(f"warn: enforcement pass failed: {exc}", file=sys.stderr)
				time.sleep(args.interval)
		else:
			try:
				_pass()
			except RuntimeError as exc:
				print(f"error: enforcement pass failed: {exc}", file=sys.stderr)
				return 1
	return 0


if __name__ == "__main__":
	sys.exit(main())
