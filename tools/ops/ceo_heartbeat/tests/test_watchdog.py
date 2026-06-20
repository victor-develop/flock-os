"""
Pure-logic unit tests for the CEO heartbeat timeout watchdog (FLO-265).

Pins the DevOps-owned decision half of the watchdog — manifest policy parse,
ISO-8601 age math, and the active/timed-out predicates — that bounds a single
CEO heartbeat run before it can paralyse the org (the FLO-264 silent-run root
cause). The enforcement layer (HTTP + process signals) lives in the script;
only the deterministic logic is asserted here, so the test runs under plain
``pytest`` with no bench, no Frappe, and no network.

The watchdog is an infra tool under ``scripts/dev/`` (not a Frappe app module),
so it is loaded by path via ``importlib`` rather than imported as a package —
keeping the Frappe app surface clean while still getting CI gate parity.
"""

from __future__ import annotations

import importlib.util
import os
import re
from pathlib import Path

import pytest

# --- Load the watchdog module by path (scripts/dev/, not a package) -------- #
# parents[4] is the repo root both in a per-slice worktree
# (.../flo/FLO-263/tools/ops/ceo_heartbeat/tests) and in a flat CI checkout
# (<repo>/tools/ops/ceo_heartbeat/tests).
_REPO_ROOT = Path(__file__).resolve().parents[4]
_SCRIPT = _REPO_ROOT / "scripts" / "dev" / "ceo-heartbeat-watchdog.py"
assert _SCRIPT.is_file(), f"watchdog script not found at {_SCRIPT}"
_spec = importlib.util.spec_from_file_location("ceo_heartbeat_watchdog", _SCRIPT)
assert _spec is not None and _spec.loader is not None
wd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wd)


# --- Fixture manifests ------------------------------------------------------ #
FULL_MANIFEST = {
	"company": {
		"heartbeat": "CEO @ */15 * * * * (coalesce_if_active, skip_missed)",
		"heartbeatConfig": {
			"agent": "ceo",
			"cron": "*/15 * * * *",
			"concurrencyPolicy": "coalesce_if_active",
			"catchUpPolicy": "skip_missed",
			"timeoutSeconds": 720,
			"timeoutAction": "terminate",
			"graceSeconds": 60,
		},
	}
}
EMPTY_MANIFEST = {"company": {}}
MANIFEST_DEFAULT_TIMEOUT = wd.DEFAULT_TIMEOUT_SECONDS


# --------------------------------------------------------------------------- #
# Manifest policy resolution
# --------------------------------------------------------------------------- #
def test_timeout_seconds_read_from_heartbeat_config():
	assert wd.heartbeat_timeout_seconds(FULL_MANIFEST) == 720


def test_timeout_seconds_falls_back_to_default_when_absent():
	assert wd.heartbeat_timeout_seconds(EMPTY_MANIFEST) == MANIFEST_DEFAULT_TIMEOUT


def test_timeout_seconds_legacy_company_field_supported():
	manifest = {"company": {"heartbeatTimeoutSeconds": 600}}
	assert wd.heartbeat_timeout_seconds(manifest) == 600


@pytest.mark.parametrize("bad", [5, 0, 3601, 99999])
def test_timeout_seconds_rejects_out_of_bounds(bad):
	manifest = {"company": {"heartbeatConfig": {"timeoutSeconds": bad}}}
	with pytest.raises(ValueError):
		wd.heartbeat_timeout_seconds(manifest)


@pytest.mark.parametrize("bad", ["720", None, 12.5, 720.0])
def test_timeout_seconds_rejects_non_int_types(bad):
	manifest = {"company": {"heartbeatConfig": {"timeoutSeconds": bad}}}
	with pytest.raises(ValueError):
		wd.heartbeat_timeout_seconds(manifest)


def test_bool_timeout_rejected_explicitly():
	manifest = {"company": {"heartbeatConfig": {"timeoutSeconds": True}}}
	with pytest.raises(ValueError):
		wd.heartbeat_timeout_seconds(manifest)


def test_grace_seconds_default_and_override():
	assert wd.heartbeat_grace_seconds(FULL_MANIFEST) == 60
	assert wd.heartbeat_grace_seconds(EMPTY_MANIFEST) == wd.DEFAULT_GRACE_SECONDS


def test_grace_seconds_rejects_negative():
	manifest = {"company": {"heartbeatConfig": {"graceSeconds": -1}}}
	with pytest.raises(ValueError):
		wd.heartbeat_grace_seconds(manifest)


def test_agent_role_default_and_override():
	assert wd.heartbeat_agent_role(FULL_MANIFEST) == "ceo"
	assert wd.heartbeat_agent_role(EMPTY_MANIFEST) == "ceo"
	manifest = {"company": {"heartbeatConfig": {"agent": "cto"}}}
	assert wd.heartbeat_agent_role(manifest) == "cto"


def test_agent_role_rejects_blank():
	manifest = {"company": {"heartbeatConfig": {"agent": "  "}}}
	with pytest.raises(ValueError):
		wd.heartbeat_agent_role(manifest)


# --------------------------------------------------------------------------- #
# ISO-8601 timestamp parsing + age math
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
	"text",
	[
		"2026-06-20T02:15:20Z",
		"2026-06-20T02:15:20.275Z",
		"2026-06-20T02:15:20+00:00",
		"2026-06-20T02:15:20",  # naive assumed UTC
	],
)
def test_parse_iso8601_accepts_common_shapes(text):
	epoch = wd.parse_iso8601_utc(text)
	assert epoch is not None and epoch > 0


@pytest.mark.parametrize("bad", ["", None, "not-a-date", "2026-13-40T99:99:99Z", 12345])
def test_parse_iso8601_rejects_garbage(bad):
	assert wd.parse_iso8601_utc(bad) is None


def test_z_suffix_and_offset_are_equivalent_utc():
	z = wd.parse_iso8601_utc("2026-06-20T02:15:20Z")
	off = wd.parse_iso8601_utc("2026-06-20T02:15:20+00:00")
	assert z == off


def test_run_age_seconds_uses_triggered_at():
	started = wd.parse_iso8601_utc("2026-06-20T02:15:20Z")
	now = started + 300.0
	run = {"triggeredAt": "2026-06-20T02:15:20Z"}
	assert wd.run_age_seconds(run, now) == 300.0


def test_run_age_seconds_falls_back_to_started_at():
	started = wd.parse_iso8601_utc("2026-06-20T02:15:20Z")
	run = {"startedAt": "2026-06-20T02:15:20Z"}
	assert wd.run_age_seconds(run, started + 60.0) == 60.0


def test_run_age_seconds_none_without_timestamp():
	assert wd.run_age_seconds({}, 1000.0) is None


def test_run_age_seconds_never_negative_for_future_start():
	started = wd.parse_iso8601_utc("2026-06-20T02:15:20Z")
	# Clock skew: "now" before the recorded start — clamp to 0, never negative.
	assert wd.run_age_seconds({"triggeredAt": "2026-06-20T02:15:20Z"}, started - 10.0) == 0.0


# --------------------------------------------------------------------------- #
# Active / timed-out predicates
# --------------------------------------------------------------------------- #
def _active_run(started_iso, run_id="run-1"):
	return {"id": run_id, "status": "running", "triggeredAt": started_iso, "completedAt": None}


def _done_run(started_iso, completed_iso, run_id="run-2"):
	return {
		"id": run_id,
		"status": "completed",
		"triggeredAt": started_iso,
		"completedAt": completed_iso,
	}


def _coalesced_run(started_iso, run_id="run-3"):
	return {
		"id": run_id,
		"status": "coalesced",
		"triggeredAt": started_iso,
		"completedAt": started_iso,
	}


def test_is_run_active_true_for_running():
	assert wd.is_run_active(_active_run("2026-06-20T02:15:20Z")) is True


def test_is_run_active_false_for_completed_and_coalesced():
	started = "2026-06-20T02:15:20Z"
	assert wd.is_run_active(_done_run(started, "2026-06-20T02:25:00Z")) is False
	assert wd.is_run_active(_coalesced_run(started)) is False


def test_is_run_active_fail_open_for_unknown_status_without_completion():
	# A novel adapter status must never hide a stuck run.
	assert wd.is_run_active({"status": "weird-new-state", "completedAt": None}) is True


def test_is_run_active_false_when_completed_at_present_regardless_of_status():
	# Status says running but a completion timestamp is set -> not active.
	run = {"status": "running", "triggeredAt": "2026-06-20T02:15:20Z", "completedAt": "2026-06-20T02:20:00Z"}
	assert wd.is_run_active(run) is False


def test_is_run_timed_out_true_when_age_at_or_above_timeout():
	started = wd.parse_iso8601_utc("2026-06-20T02:15:20Z")
	run = _active_run("2026-06-20T02:15:20Z")
	assert wd.is_run_timed_out(run, started + 720.0, 720) is True  # exactly at
	assert wd.is_run_timed_out(run, started + 721.0, 720) is True  # above


def test_is_run_timed_out_false_when_age_below_timeout():
	started = wd.parse_iso8601_utc("2026-06-20T02:15:20Z")
	run = _active_run("2026-06-20T02:15:20Z")
	assert wd.is_run_timed_out(run, started + 719.0, 720) is False


def test_is_run_timed_out_false_for_terminal_run():
	started = wd.parse_iso8601_utc("2026-06-20T02:15:20Z")
	run = _done_run("2026-06-20T02:15:20Z", "2026-06-20T02:25:00Z")
	# Way past the timeout, but it already finished -> not timed out.
	assert wd.is_run_timed_out(run, started + 10000.0, 720) is False


def test_is_run_timed_out_false_without_timestamp():
	run = {"status": "running", "completedAt": None}  # no triggeredAt
	assert wd.is_run_timed_out(run, 1_000_000_000.0, 720) is False


def test_select_timed_out_runs_returns_only_over_time_subset():
	started = wd.parse_iso8601_utc("2026-06-20T02:15:20Z")
	now = started + 1000.0  # 1000s elapsed
	runs = [
		_active_run("2026-06-20T02:15:20Z", "active-over"),  # 1000s >= 720 -> over
		_done_run("2026-06-20T02:15:20Z", "2026-06-20T02:25:00Z"),
		_coalesced_run("2026-06-20T02:15:20Z"),
		_active_run("2026-06-20T02:30:00Z", "active-fresh"),  # young
	]
	over = wd.select_timed_out_runs(runs, now, 720)
	assert len(over) == 1
	assert over[0]["id"] == "active-over"


# --------------------------------------------------------------------------- #
# ps etime parsing (macOS + Linux shapes)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
	"text,expected",
	[
		("12:00", 720.0),
		("00:30", 30.0),
		("01:02:03", 3723.0),
		("1-00:00:00", 86400.0),
		("2-12:30:00", 2 * 86400 + 12 * 3600 + 30 * 60),
	],
)
def test_parse_etime_seconds_common_shapes(text, expected):
	assert wd._parse_etime_seconds(text) == expected


@pytest.mark.parametrize("bad", ["", "n/a", ":", "1:2:3:4", "--"])
def test_parse_etime_seconds_rejects_garbage(bad):
	assert wd._parse_etime_seconds(bad) is None


# --------------------------------------------------------------------------- #
# Script-on-disk invariants
# --------------------------------------------------------------------------- #
def test_watchdog_script_is_executable():
	# Enforcement runs as a CLI; it must keep its executable bit in the repo.
	assert os.access(_SCRIPT, os.X_OK)


def test_self_test_exits_zero():
	# The runbook's pure-logic gate must pass. Subprocess keeps the CLI boundary
	# honest (main()/self_test() wired through __main__).
	import subprocess

	result = subprocess.run(
		["python3", str(_SCRIPT), "--self-test"],
		capture_output=True,
		text=True,
		check=False,
	)
	assert result.returncode == 0, result.stdout + result.stderr
	# Match "N/N passed" (count grows as cases are added) and assert no failures.
	last = [ln for ln in result.stdout.splitlines() if "passed" in ln][-1]
	m = re.search(r"(\d+)/(\d+) passed", last)
	assert m, f"self-test summary line not found: {last!r}"
	assert m.group(1) == m.group(2), f"self-test had failures: {last!r}"
