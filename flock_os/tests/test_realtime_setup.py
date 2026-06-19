"""
Realtime-handler auto-wiring harness (FLO-109, FLO-107 follow-up).

A ``bench update`` rewrites ``apps/frappe/realtime/index.js`` and silently drops
the guarded ``require`` that registers flock_os's ``join`` handler
(``scripts/dev/wire-socketio-handler.sh``). flock_os's ``after_migrate`` /
``after_install`` hooks re-run that script automatically. These tests pin both
halves of that belt-and-suspenders guard so a silent regression becomes a red
gate instead:

1. The wire script round-trips on a temp bench layout: it inserts the marker,
   a simulated reinstall (restoring the pristine file) drops it, and re-running
   restores it — idempotent + self-healing.
2. ``--check`` is a non-mutating assert (exit 0 wired / 1 absent).
3. The hook resolves the bench + drives the script and never raises on failure.

Runs under plain ``pytest`` (no bench) on any host with ``bash``.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import flock_os
from flock_os.utils import realtime_setup

REPO_ROOT = Path(flock_os.__file__).resolve().parent.parent
WIRE_SCRIPT = REPO_ROOT.joinpath(*realtime_setup.WIRE_SCRIPT_REL)

# A pristine Frappe v15 realtime index — only the anchor line matters to the
# wiring awk (``frappe_handlers(realtime, socket);``).
PRISTINE_INDEX = (
	'const frappe_handlers = require("./handlers/frappe_handlers");\n'
	"function on_connection(socket) {\n"
	"\tfrappe_handlers(realtime, socket);\n"
	'\tsocket.on("open_in_editor", async (d) => {});\n'
	"}\n"
)


def _bench(tmp_path: Path) -> Path:
	bench = tmp_path / "bench"
	(bench / "apps" / "frappe" / "realtime").mkdir(parents=True)
	index = bench / "apps" / "frappe" / "realtime" / "index.js"
	index.write_text(PRISTINE_INDEX)
	return bench


def _wire(bench: Path, *flags: str) -> subprocess.CompletedProcess:
	return subprocess.run(
		["bash", str(WIRE_SCRIPT), "--bench", str(bench), *flags],
		capture_output=True,
		text=True,
	)


def _wired(bench: Path) -> bool:
	index = bench / "apps" / "frappe" / "realtime" / "index.js"
	return "FLOCK_OS_REALTIME_HANDLER_START" in index.read_text()


def test_bench_root_from_app_path():
	app_path = "/srv/bench/apps/flock_os"
	assert realtime_setup.bench_root_from_app_path(app_path) == "/srv/bench"


def test_wire_script_round_trip_idempotent_and_self_healing(tmp_path):
	"""The exact regression FLO-109 guards: a reinstall drops the line, re-run restores it."""
	bench = _bench(tmp_path)

	# 1. Wire → marker present.
	assert _wire(bench).returncode == 0
	assert _wired(bench)

	# 2. Idempotent: wiring again is a no-op (marker still exactly one block).
	first = (bench / "apps" / "frappe" / "realtime" / "index.js").read_text()
	assert _wire(bench).returncode == 0
	assert (bench / "apps" / "frappe" / "realtime" / "index.js").read_text() == first
	assert first.count("FLOCK_OS_REALTIME_HANDLER_START") == 1

	# 3. Simulated `bench update`/reinstall rewrites index.js → marker dropped.
	(bench / "apps" / "frappe" / "realtime" / "index.js").write_text(PRISTINE_INDEX)
	assert not _wired(bench), "fixture: reinstall should drop the wiring"

	# 4. Re-wire restores it — the auto-wire hook's job.
	assert _wire(bench).returncode == 0
	assert _wired(bench)


def test_check_mode_reflects_wiring_state(tmp_path):
	bench = _bench(tmp_path)

	# Absent → --check exits 1.
	assert _wire(bench, "--check").returncode == 1

	# Present → --check exits 0 and makes no change.
	assert _wire(bench).returncode == 0
	before = (bench / "apps" / "frappe" / "realtime" / "index.js").read_text()
	assert _wire(bench, "--check").returncode == 0
	assert (bench / "apps" / "frappe" / "realtime" / "index.js").read_text() == before


def test_check_and_revert_are_mutually_exclusive(tmp_path):
	bench = _bench(tmp_path)
	assert _wire(bench, "--check", "--revert").returncode == 2


def test_rewire_hook_invokes_script_with_resolved_bench(monkeypatch, tmp_path):
	bench = tmp_path / "bench"
	app_path = bench / "apps" / "flock_os"
	app_path.mkdir(parents=True)
	captured = {}

	def fake_run(cmd, **kwargs):
		captured["cmd"] = cmd
		captured["kwargs"] = kwargs
		return SimpleNamespace(stdout="already wired", stderr="", returncode=0)

	monkeypatch.setattr(realtime_setup.subprocess, "run", fake_run)

	fake_frappe = SimpleNamespace(
		get_app_path=lambda name: str(app_path),
		logger=lambda name: SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None),
	)
	monkeypatch.setitem(sys.modules, "frappe", fake_frappe)

	realtime_setup.rewire_socketio_handler()

	expected_script = str(app_path.joinpath(*realtime_setup.WIRE_SCRIPT_REL))
	assert captured["cmd"] == ["bash", expected_script, "--bench", str(bench)]
	assert captured["kwargs"]["check"] is True
	assert captured["kwargs"]["text"] is True


def test_rewire_hook_never_raises_on_failure(monkeypatch, tmp_path):
	"""A broken wiring path must log + swallow, never break migrate/install."""
	app_path = tmp_path / "bench" / "apps" / "flock_os"
	app_path.mkdir(parents=True)

	def fake_run(cmd, **kwargs):
		raise FileNotFoundError(str(cmd[1]))

	monkeypatch.setattr(realtime_setup.subprocess, "run", fake_run)
	warnings = []
	fake_frappe = SimpleNamespace(
		get_app_path=lambda name: str(app_path),
		logger=lambda name: SimpleNamespace(
			info=lambda *a, **k: None, warning=lambda *a, **k: warnings.append(a)
		),
	)
	monkeypatch.setitem(sys.modules, "frappe", fake_frappe)

	realtime_setup.rewire_socketio_handler()  # must not raise
	assert warnings, "hook should log a warning when wiring fails"


def test_hook_wires_a_real_layout_end_to_end(monkeypatch, tmp_path):
	"""Drive the hook against a temp bench with a real flock_os app symlink+script."""
	bench = tmp_path / "bench"
	app_path = bench / "apps" / "flock_os"
	(app_path / "scripts" / "dev").mkdir(parents=True)
	# Symlink the real wire script into the temp app path so the hook finds it.
	os.symlink(WIRE_SCRIPT, app_path.joinpath(*realtime_setup.WIRE_SCRIPT_REL))
	(bench / "apps" / "frappe" / "realtime").mkdir(parents=True)
	(bench / "apps" / "frappe" / "realtime" / "index.js").write_text(PRISTINE_INDEX)

	fake_frappe = SimpleNamespace(
		get_app_path=lambda name: str(app_path),
		logger=lambda name: SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None),
	)
	monkeypatch.setitem(sys.modules, "frappe", fake_frappe)

	realtime_setup.rewire_socketio_handler()
	assert _wired(bench), "hook should leave the handler wired"
