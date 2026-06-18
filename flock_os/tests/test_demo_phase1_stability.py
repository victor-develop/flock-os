"""
Stability guard for the Phase-1 demo (FLO-81).

The companion suite ``test_demo_phase1.py`` asserts the demo's four DoD #5
scenarios pass in-process. That proves *coverage* but not *determinism*: the
demo's ``run()`` mutates module-global gateway state (``install_gateway``), so a
single in-process call can never expose a cross-run leak. FLO-81 was opened
precisely because the demo's exit code flipped once on a first run immediately
after ``qa-gate.sh`` — a transient that 95/96 subsequent runs could not
reproduce.

This module closes that gap. It drives the demo's ``main()`` (exit code) in a
**fresh subprocess** for each iteration — a clean interpreter, no shared
module-global state, no bytecode cache (``PYTHONDONTWRITEBYTECODE=1``, mirroring
the QA gate's ``ruff --no-cache`` discipline). A flaky demo now fails loudly and
repeatably in the gate rather than silently flipping. The run count is bounded
so the gate stays cheap and runs often; the full >=50-run + hashseed 0..31 sweep
lives in ``scripts/demo-phase1-stability.sh`` for sign-off evidence.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEMO = _REPO_ROOT / "scripts" / "demo_phase1.py"
# A spread of hashseeds that exercises set/dict ordering without bloating the
# gate (8 subprocess invocations ~= 1s). The full 0..31 sweep is the script.
_HASHSEEDS = (None, "0", "1", "2")


def _run_demo(hashseed: str | None) -> subprocess.CompletedProcess[str]:
	env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
	if hashseed is not None:
		env["PYTHONHASHSEED"] = hashseed
	return subprocess.run(
		[sys.executable, str(_DEMO)],
		cwd=str(_REPO_ROOT),
		env=env,
		capture_output=True,
		text=True,
		timeout=30,
	)


@pytest.mark.parametrize("hashseed", _HASHSEEDS)
def test_demo_is_deterministic_across_isolated_runs(hashseed: str | None):
	# Each iteration is a clean interpreter, so module-global gateway state
	# cannot leak between runs. Any non-zero exit or missing PASS banner fails
	# the gate loudly — the FLO-81 flake-detection contract.
	label = f"hashseed={hashseed!r}"
	proc = _run_demo(hashseed)
	stdout = proc.stdout
	assert proc.returncode == 0, (
		f"demo exited {proc.returncode} ({label}); a flaky exit code is a gate "
		f"failure.\nstdout:\n{stdout}\nstderr:\n{proc.stderr}"
	)
	assert "DEMO: PASS" in stdout, f"demo did not report PASS ({label}):\n{stdout}"
	assert "4/4 scenarios green" in stdout, f"demo did not report 4/4 green ({label}):\n{stdout}"


def test_demo_runner_script_is_executable_and_clean():
	# The hardened runner (FLO-81) must be executable and must disable bytecode
	# writes so it never leaves a stale `.pyc` for the next process to load.
	runner = _REPO_ROOT / "scripts" / "demo-phase1.sh"
	assert runner.is_file(), "scripts/demo-phase1.sh missing"
	assert os.access(runner, os.X_OK), "scripts/demo-phase1.sh must be executable"
	text = runner.read_text()
	assert "PYTHONDONTWRITEBYTECODE=1" in text, (
		"runner must export PYTHONDONTWRITEBYTECODE=1 (determinism guard, FLO-81)"
	)
