#!/usr/bin/env python3
"""
CEO heartbeat monitor CLI (FLO-266).

One-shot scrape of CEO heartbeat health against the live Paperclip control
plane. Wires :class:`tools.ops.ceo_heartbeat.monitor.PaperclipCEOHeartbeatSource`
into the pure :class:`CEOHeartbeatMonitor`, prints the snapshot, and exits with
a severity-mapped code so it drops straight into cron / launchd / an alert
wrapper:

    0  OK         — CEO heartbeat healthy, no silent runs.
    1  WARNING    — an in-flight run crossed the 15-minute silence budget, or
                    the CEO's lastHeartbeatAt is stale past the same budget.
    2  CRITICAL   — an in-flight run crossed the 30-minute budget, or the
                    org-chain health is unhealthy.
    3  CONFIG     — missing API credentials / network failure (NOT a CEO health
                    signal — fix the monitor config, do not page the CEO owner).

Output formats (``--format``):

    text        human-readable multi-line report (default)
    json        machine-readable snapshot (structured alerting / log shipping)
    prometheus  Prometheus text exposition (dashboard scrape)

Configuration (env, all optional unless noted):

    PAPERCLIP_API_URL, PAPERCLIP_API_KEY    required (auto-injected by Paperclip)
    PAPERCLIP_COMPANY_ID                    required (auto-injected)
    FLO_CEO_AGENT_ID                        CEO agent id (defaults to discovery)
    FLO_CEO_ROUTINE_ID                      CEO heartbeat routine id (auto-discovered)
    FLO_CEO_MONITOR_WARN_MIN                warning silence minutes (default 15)
    FLO_CEO_MONITOR_CRIT_MIN                critical silence minutes (default 30)
    FLO_CEO_MONITOR_RUN_LIMIT               recent-run window (default 50)

Alert mode (``--alert``): when severity is warning/critical, open idempotent
Paperclip alert issues for each silent run (routed to an agent owner, never a
human — Rule #1). Configure with:

    FLO_CEO_MONITOR_ESCALATION_AGENT_ID     alert assignee (default: discovery of
                                            the CEO's reportsTo chain — the
                                            Software Architect until FLO-267 ships)
    FLO_CEO_HEARTBEAT_ISSUE_ID              parentId for alert issues (FLO-263)
    PAPERCLIP_PROJECT_ID                    projectId for alert issues

Usage:

    scripts/dev/ceo-heartbeat-monitor.py                 # text, exit=severity
    scripts/dev/ceo-heartbeat-monitor.py --format json   # structured snapshot
    scripts/dev/ceo-heartbeat-monitor.py --format prometheus
    scripts/dev/ceo-heartbeat-monitor.py --alert         # also opens alert issues

Exit code 0/1/2 is the CEO health signal; 3 means the monitor itself is
misconfigured or could not reach the API and must not be read as "CEO healthy".
"""

from __future__ import annotations

import argparse
import os
import sys

# Make ``import tools.ops.ceo_heartbeat.monitor`` resolve when this script is
# run from anywhere. The package is not pip-installed (per AGENTS.md the import
# resolves via cwd), so add the repo root (two levels up from scripts/dev/) to
# sys.path explicitly.
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _REPO_ROOT not in sys.path:
	sys.path.insert(0, _REPO_ROOT)

from tools.ops.ceo_heartbeat.monitor import (  # noqa: E402 - path patched above
	EXIT_CONFIG_ERROR,
	CEOHeartbeatMonitor,
	PaperclipAlertIssuer,
	PaperclipCEOHeartbeatSource,
	severity_to_exit_code,
	snapshot_as_json,
	snapshot_as_prometheus,
	snapshot_as_text,
)


def _env_required(name: str) -> str:
	val = os.environ.get(name, "").strip()
	if not val:
		sys.stderr.write(f"CONFIG ERROR: environment variable {name} is required.\n")
		sys.exit(EXIT_CONFIG_ERROR)
	return val


def _env_float(name: str, default: float) -> float:
	raw = os.environ.get(name, "").strip()
	if not raw:
		return default
	try:
		return float(raw)
	except ValueError:
		sys.stderr.write(f"CONFIG ERROR: {name}={raw!r} is not a number.\n")
		sys.exit(EXIT_CONFIG_ERROR)


def _env_int(name: str, default: int) -> int:
	raw = os.environ.get(name, "").strip()
	if not raw:
		return default
	try:
		return int(raw)
	except ValueError:
		sys.stderr.write(f"CONFIG ERROR: {name}={raw!r} is not an integer.\n")
		sys.exit(EXIT_CONFIG_ERROR)


def main(argv: list[str] | None = None) -> int:
	parser = argparse.ArgumentParser(
		description="Monitor CEO heartbeat health (FLO-266). Exit code = severity.",
	)
	parser.add_argument(
		"--format",
		choices=("text", "json", "prometheus"),
		default="text",
		help="output format (default: text)",
	)
	parser.add_argument(
		"--warn-min",
		type=float,
		default=None,
		help="warning silence minutes (overrides FLO_CEO_MONITOR_WARN_MIN)",
	)
	parser.add_argument(
		"--crit-min",
		type=float,
		default=None,
		help="critical silence minutes (overrides FLO_CEO_MONITOR_CRIT_MIN)",
	)
	parser.add_argument(
		"--run-limit",
		type=int,
		default=None,
		help="recent-run window size (overrides FLO_CEO_MONITOR_RUN_LIMIT)",
	)
	parser.add_argument(
		"--alert",
		action="store_true",
		help="open idempotent Paperclip alert issues for silent runs (warning/critical severity only)",
	)
	args = parser.parse_args(argv)

	api_url = _env_required("PAPERCLIP_API_URL")
	api_key = _env_required("PAPERCLIP_API_KEY")
	company_id = _env_required("PAPERCLIP_COMPANY_ID")
	ceo_agent_id = os.environ.get("FLO_CEO_AGENT_ID", "").strip() or None
	ceo_routine_id = os.environ.get("FLO_CEO_ROUTINE_ID", "").strip() or None
	if not ceo_agent_id:
		sys.stderr.write("CONFIG ERROR: FLO_CEO_AGENT_ID is required (the CEO agent id to monitor).\n")
		sys.exit(EXIT_CONFIG_ERROR)

	warn_min = args.warn_min if args.warn_min is not None else _env_float("FLO_CEO_MONITOR_WARN_MIN", 15.0)
	crit_min = args.crit_min if args.crit_min is not None else _env_float("FLO_CEO_MONITOR_CRIT_MIN", 30.0)
	run_limit = args.run_limit if args.run_limit is not None else _env_int("FLO_CEO_MONITOR_RUN_LIMIT", 50)
	if crit_min < warn_min:
		sys.stderr.write(
			f"CONFIG ERROR: critical minutes ({crit_min}) must be >= warning minutes ({warn_min}).\n"
		)
		sys.exit(EXIT_CONFIG_ERROR)

	source = PaperclipCEOHeartbeatSource(
		api_url=api_url,
		api_key=api_key,
		company_id=company_id,
		ceo_agent_id=ceo_agent_id,
		ceo_routine_id=ceo_routine_id,
	)
	monitor = CEOHeartbeatMonitor(
		source=source,
		warning_minutes=warn_min,
		critical_minutes=crit_min,
		run_limit=run_limit,
	)

	try:
		snap = monitor.evaluate_health()
	except Exception as exc:  # noqa: BLE001 - surface as config/network error
		sys.stderr.write(f"CONFIG ERROR: could not read CEO heartbeat health from Paperclip: {exc}\n")
		sys.exit(EXIT_CONFIG_ERROR)

	if args.format == "json":
		print(snapshot_as_json(snap))
	elif args.format == "prometheus":
		print(snapshot_as_prometheus(snap), end="")
	else:
		print(snapshot_as_text(snap))

	if args.alert and snap.silent_run_alerts:
		_issue_alerts(snap, company_id, ceo_agent_id, api_url, api_key)

	return severity_to_exit_code(snap.severity)


def _issue_alerts(snap, company_id, ceo_agent_id, api_url, api_key) -> None:
	"""Open idempotent Paperclip alert issues for each silent run (FLO-266)."""
	escalation_agent_id = os.environ.get("FLO_CEO_MONITOR_ESCALATION_AGENT_ID", "").strip()
	if not escalation_agent_id:
		# Default: discover the CEO's recovery owner from the org chain. The CEO
		# reports to nobody, so the alert routes to the Software Architect (the
		# architect handled FLO-264 and owns CEO recovery until FLO-267 ships).
		escalation_agent_id = _discover_escalation_owner(api_url, api_key, ceo_agent_id)
	parent_issue_id = os.environ.get("FLO_CEO_HEARTBEAT_ISSUE_ID", "").strip() or None
	project_id = os.environ.get("PAPERCLIP_PROJECT_ID", "").strip() or None
	issuer = PaperclipAlertIssuer(
		api_url=api_url,
		api_key=api_key,
		company_id=company_id,
		escalation_agent_id=escalation_agent_id,
		parent_issue_id=parent_issue_id,
		project_id=project_id,
	)
	try:
		created = issuer.issue_for(snap.silent_run_alerts)
	except Exception as exc:  # noqa: BLE001 - alerting must never mask the health exit
		sys.stderr.write(f"ALERT ERROR: could not open Paperclip alert issues: {exc}\n")
		return
	for issue in created:
		ident = issue.get("identifier") or issue.get("id")
		sys.stderr.write(f"ALERT OPENED: {ident} ({issue.get('priority')})\n")
	if not created:
		sys.stderr.write("ALERT: silent run(s) already have open alert issues (deduped).\n")


def _discover_escalation_owner(api_url, api_key, ceo_agent_id) -> str:
	"""Default alert owner: the CEO's first direct report by role precedence.

	The CEO has no manager, so a manager-escalation is impossible. Route to the
	Software Architect (role ``cto``) — they own CEO run recovery (FLO-264
	precedent, and FLO-267 will codify auto-restart under them). Falls back to
	the first agent that is not the CEO if no architect is found.
	"""
	import json as _json
	from urllib.error import HTTPError, URLError
	from urllib.request import Request, urlopen

	url = f"{api_url.rstrip('/')}/api/companies/{os.environ['PAPERCLIP_COMPANY_ID']}/agents"
	req = Request(url, headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"})
	try:
		with urlopen(req, timeout=15) as resp:
			agents = _json.loads(resp.read().decode("utf-8"))
	except (HTTPError, URLError, OSError, ValueError):
		# Cannot discover — fail closed: route back to the CEO so the alert is
		# at least visible in-thread, and surface the config gap in stderr.
		sys.stderr.write(
			"CONFIG WARNING: could not discover escalation owner; routing alert "
			f"to CEO ({ceo_agent_id}). Set FLO_CEO_MONITOR_ESCALATION_AGENT_ID.\n"
		)
		return ceo_agent_id
	if not isinstance(agents, list):
		return ceo_agent_id
	for role_preference in ("cto", "software_architect"):
		for a in agents:
			if isinstance(a, dict) and a.get("role") == role_preference and a.get("id") != ceo_agent_id:
				return a["id"]
	for a in agents:
		if isinstance(a, dict) and a.get("id") != ceo_agent_id:
			return a["id"]
	return ceo_agent_id


if __name__ == "__main__":
	raise SystemExit(main())
