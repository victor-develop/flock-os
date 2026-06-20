"""
CEO heartbeat monitoring + observability lock (FLO-266).

Pins the DevOps-owned detection half of the CEO silent-run defense-in-depth
([FLO-265](/FLO/issues/FLO-265) timeout / [FLO-266](/FLO/issues/FLO-266) this /
[FLO-267](/FLO/issues/FLO-267) recovery):

* Silent / stuck-run detection — an in-flight run crossing the 15m warning /
  30m critical thresholds (the FLO-264 incident shape: a run sitting in
  ``issue_created`` with ``completedAt=null`` for >1h).
* Completion-time trends — p50/p95/max over completed runs (slowdown smoke).
* CEO liveness severity — ``lastHeartbeatAt`` staleness + org-chain health.
* Snapshot composition + severity reduction (the dashboard / scrape surface).
* The :class:`PaperclipCEOHeartbeatSource` adapter normalises the live API
  payload via an injectable HTTP fetcher (no network in CI).
* Prometheus + JSON + text rendering stays well-formed.

Runs under plain ``pytest`` (no bench, no network): pure detection math over
:class:`StaticCEOHeartbeatSource`, and the Paperclip adapter over a canned-fetcher.
"""

from __future__ import annotations

import json
from typing import Any

import tools.ops.ceo_heartbeat.monitor as chm
from tools.ops.ceo_heartbeat.monitor import (
	ACTIVE_RUN_STATUSES,
	DEFAULT_CRITICAL_SILENCE_MINUTES,
	DEFAULT_WARNING_SILENCE_MINUTES,
	EXIT_CRITICAL,
	EXIT_OK,
	EXIT_WARNING,
	SEVERITY_CRITICAL,
	SEVERITY_OK,
	SEVERITY_WARNING,
	CEOAgentState,
	CEOHealthSnapshot,
	CEOHeartbeatMonitor,
	CompletionStats,
	HeartbeatRun,
	PaperclipAlertIssuer,
	PaperclipCEOHeartbeatSource,
	SilentRunAlert,
	StaticCEOHeartbeatSource,
	compute_completion_stats,
	detect_silent_runs,
	severity_to_exit_code,
	snapshot_as_json,
	snapshot_as_prometheus,
	snapshot_as_text,
)

NOW = 1_700_000_000.0  # fixed epoch so age math is deterministic


def _run(
	*,
	id: str,
	status: str,
	triggered_offset_min: float,
	completed_offset_min: float | None = None,
	linked_issue_identifier: str | None = "FLO-1",
	coalesced_into_run_id: str | None = None,
) -> HeartbeatRun:
	"""Build a run relative to :data:`NOW` (offsets in minutes, negative = past)."""
	return HeartbeatRun(
		id=id,
		status=status,
		triggered_at_epoch=NOW + triggered_offset_min * 60,
		completed_at_epoch=(NOW + completed_offset_min * 60) if completed_offset_min is not None else None,
		linked_issue_id=None,
		linked_issue_identifier=linked_issue_identifier,
		linked_issue_title="t",
		linked_issue_status=None,
		coalesced_into_run_id=coalesced_into_run_id,
		failure_reason=None,
	)


def _healthy_agent() -> CEOAgentState:
	return CEOAgentState(
		id="ceo",
		name="CEO",
		status="idle",
		last_heartbeat_at_epoch=NOW - 60,  # 1 min ago — fresh
		updated_at_epoch=NOW - 60,
		org_chain_health_status="healthy",
		org_chain_health_reason="healthy",
	)


# --------------------------------------------------------------------------- #
# Timestamp parsing (defensive — bad input must never break a scrape).
# --------------------------------------------------------------------------- #
def test_parse_iso8601_z_suffix_and_epoch_and_garbage():
	assert chm._parse_iso8601_to_epoch("2026-06-20T02:15:20.000Z") is not None
	assert chm._parse_iso8601_to_epoch(1_700_000_000) == 1_700_000_000.0
	assert chm._parse_iso8601_to_epoch(None) is None
	assert chm._parse_iso8601_to_epoch("") is None
	assert chm._parse_iso8601_to_epoch("not-a-date") is None


# --------------------------------------------------------------------------- #
# Silent-run detection — the FLO-266 acceptance core.
# --------------------------------------------------------------------------- #
def test_terminal_run_is_never_silent():
	r = _run(id="r1", status="completed", triggered_offset_min=-60, completed_offset_min=-55)
	assert detect_silent_runs((r,), NOW) == ()


def test_active_run_under_warning_threshold_is_not_silent():
	# 5 min old, threshold 15m -> no alert yet.
	r = _run(id="r2", status="issue_created", triggered_offset_min=-5)
	assert detect_silent_runs((r,), NOW) == ()


def test_active_run_at_warning_threshold_alerts_warning():
	r = _run(id="r3", status="issue_created", triggered_offset_min=-DEFAULT_WARNING_SILENCE_MINUTES)
	alerts = detect_silent_runs((r,), NOW)
	assert len(alerts) == 1
	assert alerts[0].severity == SEVERITY_WARNING
	assert alerts[0].run.id == "r3"
	assert alerts[0].age_minutes == DEFAULT_WARNING_SILENCE_MINUTES


def test_active_run_over_critical_threshold_alerts_critical():
	r = _run(id="r4", status="issue_created", triggered_offset_min=-DEFAULT_CRITICAL_SILENCE_MINUTES - 1)
	alerts = detect_silent_runs((r,), NOW)
	assert len(alerts) == 1
	assert alerts[0].severity == SEVERITY_CRITICAL


def test_coalesced_run_is_not_silent_even_when_old():
	# A coalesced overlap points at the real active run; it must not double-alert.
	r = _run(
		id="r5",
		status="issue_created",
		triggered_offset_min=-40,
		coalesced_into_run_id="r-real",
	)
	assert detect_silent_runs((r,), NOW) == ()


def test_completed_status_with_null_completed_at_does_not_alert_on_unknown_status():
	# Defensive: a status outside the active set is skipped even if completedAt is null.
	r = _run(id="r6", status="some-new-terminal-state", triggered_offset_min=-40)
	assert r.status not in ACTIVE_RUN_STATUSES
	assert detect_silent_runs((r,), NOW) == ()


def test_silent_alerts_ordered_oldest_first():
	oldest = _run(id="old", status="issue_created", triggered_offset_min=-50)
	newest = _run(id="new", status="issue_created", triggered_offset_min=-32)
	alerts = detect_silent_runs((newest, oldest), NOW)
	# Both critical; oldest (largest age) leads.
	assert [a.run.id for a in alerts] == ["old", "new"]


def test_custom_thresholds_override_defaults():
	r = _run(id="r7", status="issue_created", triggered_offset_min=-5)
	# 5 min age with a 4m warning threshold -> warning fires.
	alerts = detect_silent_runs((r,), NOW, warning_minutes=4, critical_minutes=60)
	assert len(alerts) == 1
	assert alerts[0].severity == SEVERITY_WARNING


# --------------------------------------------------------------------------- #
# Completion-time trends.
# --------------------------------------------------------------------------- #
def test_completion_stats_empty_when_no_completed_runs():
	stats = compute_completion_stats((), now_epoch=NOW)
	assert stats.count == 0
	assert stats.p50_minutes is None
	assert stats.max_minutes is None


def test_completion_stats_percentiles_and_failures():
	# 3 completed runs: 2m, 4m, 6m; plus 1 failed (10m); plus 1 coalesced (skipped).
	runs = (
		_run(id="ok1", status="completed", triggered_offset_min=-10, completed_offset_min=-8),
		_run(id="ok2", status="completed", triggered_offset_min=-20, completed_offset_min=-16),
		_run(id="ok3", status="completed", triggered_offset_min=-30, completed_offset_min=-24),
		_run(id="fail", status="failed", triggered_offset_min=-40, completed_offset_min=-30),
		_run(id="skip", status="coalesced", triggered_offset_min=-50, completed_offset_min=-49),
	)
	stats = compute_completion_stats(runs, now_epoch=NOW)
	# 4 durations: 2, 4, 6, 10 minutes -> sorted [2,4,6,10].
	assert stats.count == 4
	assert stats.failure_count == 1
	assert stats.durations_minutes == (2.0, 4.0, 6.0, 10.0)
	assert stats.max_minutes == 10.0
	# nearest-rank p50 = ceil(0.5*4)=2nd -> 4.0 ; p95 = ceil(0.95*4)=4th -> 10.0
	assert stats.p50_minutes == 4.0
	assert stats.p95_minutes == 10.0
	assert stats.mean_minutes == 5.5


def test_completion_stats_ignores_negative_duration():
	# completedAt before triggeredAt (clock skew) -> skipped, not negative.
	runs = (
		_run(id="skew", status="completed", triggered_offset_min=-10, completed_offset_min=-12),
		_run(id="ok", status="completed", triggered_offset_min=-10, completed_offset_min=-8),
	)
	stats = compute_completion_stats(runs, now_epoch=NOW)
	assert stats.count == 1
	assert stats.max_minutes == 2.0


# --------------------------------------------------------------------------- #
# Liveness severity + severity reduction.
# --------------------------------------------------------------------------- #
def test_liveness_ok_when_heartbeat_fresh():
	assert chm._liveness_severity(_healthy_agent(), NOW) == SEVERITY_OK


def test_liveness_warning_when_heartbeat_stale_over_warning_budget():
	agent = CEOAgentState(
		id="ceo",
		name="CEO",
		status="idle",
		last_heartbeat_at_epoch=NOW - (DEFAULT_WARNING_SILENCE_MINUTES + 1) * 60,
		updated_at_epoch=None,
		org_chain_health_status="healthy",
	)
	assert chm._liveness_severity(agent, NOW) == SEVERITY_WARNING


def test_liveness_critical_when_org_chain_unhealthy():
	agent = CEOAgentState(
		id="ceo",
		name="CEO",
		status="idle",
		last_heartbeat_at_epoch=NOW - 30,
		updated_at_epoch=None,
		org_chain_health_status="unhealthy",
	)
	# Unhealthy chain is critical regardless of heartbeat freshness.
	assert chm._liveness_severity(agent, NOW) == SEVERITY_CRITICAL


def test_severity_to_exit_code_mapping():
	assert severity_to_exit_code(SEVERITY_OK) == EXIT_OK
	assert severity_to_exit_code(SEVERITY_WARNING) == EXIT_WARNING
	assert severity_to_exit_code(SEVERITY_CRITICAL) == EXIT_CRITICAL


# --------------------------------------------------------------------------- #
# Monitor composition — the CEOHealthSnapshot surface.
# --------------------------------------------------------------------------- #
def _monitor(runs, agent=None) -> CEOHeartbeatMonitor:
	return CEOHeartbeatMonitor(
		source=StaticCEOHeartbeatSource(agent or _healthy_agent(), tuple(runs)),
		clock=lambda: NOW,
	)


def test_monitor_healthy_snapshot_has_no_alerts():
	mon = _monitor(
		(
			_run(id="ok", status="completed", triggered_offset_min=-10, completed_offset_min=-5),
			_run(id="active", status="issue_created", triggered_offset_min=-3),
		)
	)
	snap = mon.evaluate_health()
	assert snap.severity == SEVERITY_OK
	assert snap.silent_run_alerts == ()
	assert len(snap.active_runs) == 1
	assert snap.active_runs[0].id == "active"
	assert snap.completion_stats.count == 1
	# current operation = most-recent active run.
	assert snap.current_operation is not None
	assert snap.current_operation.id == "active"
	assert snap.runs_inspected == 2


def test_monitor_critical_when_silent_run_over_critical_threshold():
	mon = _monitor((_run(id="stuck", status="issue_created", triggered_offset_min=-40),))
	snap = mon.evaluate_health()
	assert snap.severity == SEVERITY_CRITICAL
	assert len(snap.silent_run_alerts) == 1
	assert snap.silent_run_alerts[0].severity == SEVERITY_CRITICAL


def test_monitor_current_operation_falls_back_to_most_recent_run_when_none_active():
	mon = _monitor((_run(id="done", status="completed", triggered_offset_min=-5, completed_offset_min=-1),))
	snap = mon.evaluate_health()
	assert snap.active_runs == ()
	assert snap.current_operation is not None
	assert snap.current_operation.id == "done"


def test_monitor_critical_silent_beats_warning_liveness():
	# Stuck run (critical) + stale heartbeat (warning) -> overall critical.
	agent = CEOAgentState(
		id="ceo",
		name="CEO",
		status="idle",
		last_heartbeat_at_epoch=NOW - (DEFAULT_WARNING_SILENCE_MINUTES + 1) * 60,
		updated_at_epoch=None,
		org_chain_health_status="healthy",
	)
	mon = _monitor((_run(id="stuck", status="issue_created", triggered_offset_min=-40),), agent=agent)
	snap = mon.evaluate_health()
	assert snap.severity == SEVERITY_CRITICAL


def test_monitor_respects_run_limit_window():
	passed = []

	class CountingSource:
		def __init__(self, n):
			self.n = n

		def fetch(self, run_limit=50):
			passed.append(run_limit)
			# Mirror Static/Paperclip sources: honour the requested window.
			all_runs = tuple(
				_run(id=f"r{i}", status="completed", triggered_offset_min=-i, completed_offset_min=-i + 1)
				for i in range(self.n)
			)
			return _healthy_agent(), all_runs[-run_limit:] if run_limit < len(all_runs) else all_runs

	mon = CEOHeartbeatMonitor(source=CountingSource(80), run_limit=25, clock=lambda: NOW)
	snap = mon.evaluate_health()
	assert passed == [25]
	assert snap.runs_inspected == 25


# --------------------------------------------------------------------------- #
# Rendering — text / JSON / Prometheus (the dashboard surfaces).
# --------------------------------------------------------------------------- #
def test_snapshot_text_mentions_severity_and_current_operation():
	mon = _monitor(
		(
			_run(
				id="active",
				status="issue_created",
				triggered_offset_min=-3,
				linked_issue_identifier="FLO-999",
			),
		)
	)
	txt = snapshot_as_text(mon.evaluate_health())
	assert "CEO heartbeat health: OK" in txt
	assert "FLO-999" in txt  # current operation
	assert "active runs: 1" in txt


def test_snapshot_json_is_valid_and_round_trips_severity():
	mon = _monitor(
		(_run(id="stuck", status="issue_created", triggered_offset_min=-40, linked_issue_identifier="FLO-9"),)
	)
	payload = json.loads(snapshot_as_json(mon.evaluate_health()))
	assert payload["severity"] == SEVERITY_CRITICAL
	assert payload["current_operation"]["linked_issue"] == "FLO-9"
	assert payload["silent_run_alerts"][0]["age_minutes"] == 40.0
	assert payload["agent"]["name"] == "CEO"


def test_snapshot_prometheus_has_key_gauges_and_silent_run_series():
	mon = _monitor(
		(_run(id="stuck", status="issue_created", triggered_offset_min=-40, linked_issue_identifier="FLO-9"),)
	)
	prom = snapshot_as_prometheus(mon.evaluate_health())
	assert "ceo_health_severity 2" in prom  # critical
	assert "ceo_active_run_count 1" in prom
	assert "ceo_silent_run_age_minutes" in prom
	assert 'linked_issue="FLO-9"' in prom


# --------------------------------------------------------------------------- #
# Paperclip adapter — normalises the live API payload via injected fetcher.
# --------------------------------------------------------------------------- #
def test_paperclip_source_normalises_agent_and_runs():
	calls: list[str] = []

	def fake_get(url, headers):
		calls.append(url)
		if url.endswith("/api/agents/ceo-1"):
			return json.dumps(
				{
					"id": "ceo-1",
					"name": "CEO",
					"status": "idle",
					"lastHeartbeatAt": "2026-06-20T02:15:20.000Z",
					"updatedAt": "2026-06-20T02:15:20.000Z",
					"orgChainHealth": {"status": "healthy", "reason": "healthy"},
				}
			)
		if url.endswith("/api/routines/r-1/runs?limit=50"):
			return json.dumps(
				[
					{
						"id": "run-1",
						"status": "issue_created",
						"triggeredAt": "2026-06-20T02:15:20.000Z",
						"completedAt": None,
						"linkedIssueId": "i1",
						"linkedIssue": {
							"identifier": "FLO-263",
							"title": "heartbeat",
							"status": "in_progress",
						},
						"coalescedIntoRunId": None,
						"failureReason": None,
					},
					{
						"id": "run-2",
						"status": "completed",
						"triggeredAt": "2026-06-20T01:00:00.000Z",
						"completedAt": "2026-06-20T01:05:00.000Z",
						"linkedIssue": {"identifier": "FLO-260", "title": "x", "status": "done"},
					},
				]
			)
		raise AssertionError(f"unexpected url {url}")

	src = PaperclipCEOHeartbeatSource(
		api_url="https://example.test",
		api_key="k",
		company_id="c",
		ceo_agent_id="ceo-1",
		ceo_routine_id="r-1",
		http_get=fake_get,
	)
	agent, runs = src.fetch()
	assert agent.name == "CEO"
	assert agent.status == "idle"
	assert agent.org_chain_health_status == "healthy"
	assert agent.last_heartbeat_at_epoch is not None
	assert len(runs) == 2
	assert runs[0].id == "run-1"
	assert runs[0].status == "issue_created"
	assert runs[0].completed_at_epoch is None
	assert runs[0].linked_issue_identifier == "FLO-263"
	assert runs[1].completed_at_epoch is not None
	# Exactly the two documented endpoints, no routine discovery needed.
	assert [c.split("/api")[-1] for c in calls] == ["/agents/ceo-1", "/routines/r-1/runs?limit=50"]


def test_paperclip_source_discovers_routine_id_when_not_configured():
	def fake_get(url, headers):
		if url.endswith("/api/agents/ceo-1"):
			return json.dumps({"id": "ceo-1", "name": "CEO", "status": "idle"})
		if url.endswith("/api/companies/c/routines"):
			return json.dumps(
				[
					{"id": "other", "assigneeAgentId": "someone-else", "status": "active"},
					{"id": "r-1", "assigneeAgentId": "ceo-1", "status": "active"},
				]
			)
		if url.endswith("/api/routines/r-1/runs?limit=50"):
			return "[]"
		raise AssertionError(f"unexpected url {url}")

	src = PaperclipCEOHeartbeatSource(
		api_url="https://example.test",
		api_key="k",
		company_id="c",
		ceo_agent_id="ceo-1",
		ceo_routine_id=None,
		http_get=fake_get,
	)
	agent, runs = src.fetch()
	assert agent.name == "CEO"
	# Discovered routine id cached on the instance.
	assert src.ceo_routine_id == "r-1"
	assert runs == ()


def test_paperclip_source_raises_when_no_ceo_routine_found():
	def fake_get(url, headers):
		if url.endswith("/api/agents/ceo-1"):
			return json.dumps({"id": "ceo-1", "name": "CEO", "status": "idle"})
		if url.endswith("/api/companies/c/routines"):
			return json.dumps([{"id": "x", "assigneeAgentId": "other", "status": "active"}])
		raise AssertionError(f"unexpected url {url}")

	src = PaperclipCEOHeartbeatSource(
		api_url="https://example.test",
		api_key="k",
		company_id="c",
		ceo_agent_id="ceo-1",
		ceo_routine_id=None,
		http_get=fake_get,
	)
	try:
		src.fetch()
	except RuntimeError as exc:
		assert "No active routine assigned to CEO" in str(exc)
	else:
		raise AssertionError("expected RuntimeError for missing CEO routine")


def test_default_http_get_maps_http_error_to_runtime_error(monkeypatch):
	# The default fetcher imports urlopen lazily at call time, so patching the
	# urllib.request.urlopen attribute is picked up inside default_http_get.
	import urllib.error
	import urllib.request

	def fake_urlopen(req, timeout=15):
		raise urllib.error.HTTPError(req.full_url, 500, "Server Error", {}, None)

	monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
	try:
		chm.default_http_get("https://example.test/x", {})
	except RuntimeError as exc:
		assert "HTTP 500" in str(exc)
	else:
		raise AssertionError("expected RuntimeError from default_http_get")


def test_default_http_get_maps_network_error_to_runtime_error(monkeypatch):
	import urllib.error
	import urllib.request

	def fake_urlopen(req, timeout=15):
		raise urllib.error.URLError("dns fail")

	monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
	try:
		chm.default_http_get("https://example.test/x", {})
	except RuntimeError as exc:
		assert "network error" in str(exc)
	else:
		raise AssertionError("expected RuntimeError from default_http_get")


# --------------------------------------------------------------------------- #
# default_http_post — JSON writer + error mapping.
# --------------------------------------------------------------------------- #
class _FakeResponse:
	def __init__(self, body=b"{}"):
		self._body = body

	def __enter__(self):
		return self

	def __exit__(self, *a):
		return False

	def read(self):
		return self._body


def test_default_http_post_success_serialises_json(monkeypatch):
	import urllib.request

	captured = {}

	def fake_urlopen(req, timeout=20):
		captured["url"] = req.full_url
		captured["method"] = req.get_method()
		captured["data"] = req.data
		captured["headers"] = dict(req.header_items())
		return _FakeResponse(b'{"identifier": "FLO-999"}')

	monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
	out = chm.default_http_post("https://example.test/x", {"h": "1"}, {"a": 1})
	assert out == {"identifier": "FLO-999"}
	assert captured["method"] == "POST"
	# Body was JSON-encoded with the dict.
	import json as _json

	assert _json.loads(captured["data"]) == {"a": 1}


def test_default_http_post_maps_http_error(monkeypatch):
	import urllib.error
	import urllib.request

	def fake_urlopen(req, timeout=20):
		raise urllib.error.HTTPError(req.full_url, 422, "Bad", {}, None)

	monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
	try:
		chm.default_http_post("https://example.test/x", {"h": "1"}, {})
	except RuntimeError as exc:
		assert "HTTP 422" in str(exc)
	else:
		raise AssertionError("expected RuntimeError")


# --------------------------------------------------------------------------- #
# PaperclipAlertIssuer — idempotent alert issue creation (FLO-266).
# --------------------------------------------------------------------------- #
def _alert(run_id="run-x", severity=SEVERITY_CRITICAL, age=40.0, linked="FLO-9") -> SilentRunAlert:
	run = HeartbeatRun(
		id=run_id,
		status="issue_created",
		triggered_at_epoch=NOW - age * 60,
		completed_at_epoch=None,
		linked_issue_id="i",
		linked_issue_identifier=linked,
		linked_issue_title="t",
		linked_issue_status="in_progress",
		coalesced_into_run_id=None,
		failure_reason=None,
	)
	return SilentRunAlert(run=run, age_minutes=age, severity=severity)


def _issuer(get_payload, posted=None):
	"""Build an issuer with an in-memory fake GET + capture POST calls."""
	posts: list[dict[str, Any]] = []

	def fake_get(url, headers):
		if isinstance(get_payload, Exception):
			raise get_payload
		return get_payload

	def fake_post(url, headers, body):
		posts.append({"url": url, "body": body})
		if isinstance(posted, Exception):
			raise posted
		return posted if posted is not None else {"identifier": "FLO-NEW", "id": "new-1"}

	issuer = PaperclipAlertIssuer(
		api_url="https://example.test",
		api_key="k",
		company_id="c",
		escalation_agent_id="architect-1",
		parent_issue_id="FLO-263",
		project_id="proj-1",
		http_get=fake_get,
		http_post=fake_post,
	)
	return issuer, posts


def test_alert_issuer_creates_issue_when_no_existing_alert():
	issuer, posts = _issuer(get_payload=[], posted={"identifier": "FLO-NEW"})
	created = issuer.issue_for((_alert(run_id="run-1"),))
	assert len(created) == 1
	assert created[0]["identifier"] == "FLO-NEW"
	assert len(posts) == 1
	body = posts[0]["body"]
	assert body["assigneeAgentId"] == "architect-1"
	assert body["parentId"] == "FLO-263"
	assert body["projectId"] == "proj-1"
	assert body["status"] == "blocked"
	assert body["priority"] == "critical"  # critical severity -> critical priority
	assert "run-1" in body["title"]
	assert "run:run-1" in body["labels"]
	assert "/issues/FLO-267" in body["description"]  # recovery pointer link


def test_alert_issuer_priority_high_for_warning():
	issuer, posts = _issuer(get_payload=[], posted={"identifier": "FLO-NEW"})
	issuer.issue_for((_alert(severity=SEVERITY_WARNING, age=20.0),))
	# Priority is read off the POSTed body (the create request), not the response.
	assert posts[0]["body"]["priority"] == "high"


def test_alert_issuer_dedupes_when_open_alert_mentions_run_id():
	# An existing OPEN issue whose title mentions the run id -> dedupe.
	open_alert = [{"title": "[CRITICAL] CEO silent heartbeat run run-1 (40m)", "status": "blocked"}]
	issuer, posts = _issuer(get_payload=open_alert)
	created = issuer.issue_for((_alert(run_id="run-1"),))
	assert created == ()
	assert posts == []  # no POST made


def test_alert_issuer_does_not_dedupe_closed_issue():
	# A DONE issue mentioning the run id does NOT block a fresh alert.
	closed = [{"title": "old CEO silent heartbeat run run-1", "status": "done"}]
	issuer, posts = _issuer(get_payload=closed, posted={"identifier": "FLO-NEW"})
	created = issuer.issue_for((_alert(run_id="run-1"),))
	assert len(created) == 1


def test_alert_issuer_handles_dict_items_payload_shape():
	# Some list endpoints wrap results in {"items": [...]}.
	payload = {"items": [{"title": "...run run-2...", "status": "in_review"}]}
	issuer, posts = _issuer(get_payload=payload)
	created = issuer.issue_for((_alert(run_id="run-2"),))
	assert created == ()


def test_alert_issuer_creates_one_per_silent_run_and_dedupes_per_run():
	# Two silent runs; only run-a already has an open alert.
	payload = [{"title": "run-a", "status": "blocked"}]
	issuer, posts = _issuer(get_payload=payload, posted={"identifier": "FLO-NEW"})
	created = issuer.issue_for((_alert(run_id="run-a"), _alert(run_id="run-b")))
	# Only run-b is created; run-a deduped.
	assert len(created) == 1
	assert len(posts) == 1
	assert "run-b" in posts[0]["body"]["title"]


def test_alert_issuer_alert_body_falls_back_to_run_id_when_no_linked_issue():
	issuer, _ = _issuer(get_payload=[])
	body = issuer._alert_body(
		SilentRunAlert(
			run=HeartbeatRun(
				id="run-z",
				status="issue_created",
				triggered_at_epoch=NOW - 40 * 60,
				completed_at_epoch=None,
				linked_issue_identifier=None,
				linked_issue_title=None,
				linked_issue_status=None,
			),
			age_minutes=40.0,
			severity=SEVERITY_CRITICAL,
		)
	)
	# Fallback "what" is the run id; description references it.
	assert "run-z" in body["title"]


def test_alert_issuer_omits_parent_and_project_when_not_set():
	issuer, _ = _issuer(get_payload=[])
	issuer.parent_issue_id = None
	issuer.project_id = None
	body = issuer._alert_body(_alert())
	assert "parentId" not in body
	assert "projectId" not in body


def test_alert_issuer_skips_non_dict_post_payload():
	# A non-dict POST response is skipped (not appended to created).
	issuer, posts = _issuer(get_payload=[], posted=["not", "a", "dict"])
	created = issuer.issue_for((_alert(),))
	assert created == ()
	assert len(posts) == 1  # the POST was still made


def test_alert_issuer_treats_failed_search_as_no_existing_alert():
	# Search raising -> treated as "no existing alert" (better duplicate than drop).
	issuer, posts = _issuer(get_payload=RuntimeError("search boom"), posted={"identifier": "FLO-NEW"})
	created = issuer.issue_for((_alert(),))
	assert len(created) == 1


# --------------------------------------------------------------------------- #
# Rendering edge cases (text / json / prometheus branches).
# --------------------------------------------------------------------------- #
def _snap(agent=None, active=(), alerts=(), stats=None, current=None) -> CEOHealthSnapshot:
	return CEOHealthSnapshot(
		taken_at_epoch=NOW,
		severity=SEVERITY_CRITICAL if alerts else SEVERITY_OK,
		agent=agent or _healthy_agent(),
		active_runs=tuple(active),
		silent_run_alerts=tuple(alerts),
		completion_stats=stats
		or CompletionStats(
			count=0,
			failure_count=0,
			p50_minutes=None,
			p95_minutes=None,
			max_minutes=None,
			mean_minutes=None,
			durations_minutes=(),
		),
		current_operation=current,
		runs_inspected=0,
	)


def test_text_rendering_with_silent_runs_and_no_active():
	# Silent alerts render the SILENT RUNS block (a silent run is itself an
	# active run, so the active/none branches are skipped).
	txt = snapshot_as_text(_snap(alerts=(_alert(run_id="run-1", linked="FLO-9"),)))
	assert "SILENT RUNS (1)" in txt
	assert "[CRITICAL]" in txt
	assert "run-1" in txt


def test_text_rendering_active_runs_none_when_no_alerts_and_no_active():
	# No alerts AND no active runs -> the "active runs: none" branch.
	txt = snapshot_as_text(_snap(active=(), alerts=()))
	assert "active runs: none" in txt


def test_text_rendering_subminute_duration_formats_as_seconds():
	stats = CompletionStats(
		count=1,
		failure_count=0,
		p50_minutes=0.2,
		p95_minutes=0.5,
		max_minutes=0.5,
		mean_minutes=0.3,
		durations_minutes=(0.2,),
	)
	txt = snapshot_as_text(_snap(stats=stats))
	# 0.2m -> 12s ; rendered with 's' suffix.
	assert "12s" in txt


def test_json_rendering_handles_null_current_operation():
	payload = json.loads(snapshot_as_json(_snap(current=None)))
	assert payload["current_operation"] is None
	assert payload["completion"]["count"] == 0


def test_prometheus_skips_summary_when_no_completed_runs():
	prom = snapshot_as_prometheus(_snap())
	assert "ceo_heartbeat_completion_seconds" not in prom
	assert "ceo_health_severity 0" in prom  # ok severity, no alerts


# --------------------------------------------------------------------------- #
# PaperclipSource — defensive payload branches.
# --------------------------------------------------------------------------- #
def test_paperclip_source_rejects_non_dict_agent_payload():
	def fake_get(url, headers):
		# Valid JSON but not an object (a list) -> rejected by the adapter.
		return "[]"

	src = PaperclipCEOHeartbeatSource(
		api_url="https://example.test",
		api_key="k",
		company_id="c",
		ceo_agent_id="ceo-1",
		ceo_routine_id="r-1",
		http_get=fake_get,
	)
	try:
		src.fetch()
	except RuntimeError as exc:
		assert "Unexpected agent payload" in str(exc)
	else:
		raise AssertionError("expected RuntimeError")


def test_paperclip_source_skips_malformed_run_entries():
	def fake_get(url, headers):
		if url.endswith("/api/agents/ceo-1"):
			return json.dumps({"id": "ceo-1", "name": "CEO", "status": "idle"})
		# A non-dict entry and an entry missing id -> both skipped.
		return json.dumps(["not-a-dict", {"status": "completed"}, {"id": "ok", "status": "completed"}])

	src = PaperclipCEOHeartbeatSource(
		api_url="https://example.test",
		api_key="k",
		company_id="c",
		ceo_agent_id="ceo-1",
		ceo_routine_id="r-1",
		http_get=fake_get,
	)
	_, runs = src.fetch()
	assert len(runs) == 1
	assert runs[0].id == "ok"


def test_parse_iso8601_handles_naive_timestamp():
	# Naive ISO (no Z / offset) is assumed UTC and parses.
	val = chm._parse_iso8601_to_epoch("2026-06-20T02:15:20.000")
	assert val is not None
