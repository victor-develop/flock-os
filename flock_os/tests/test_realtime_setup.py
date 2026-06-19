"""
Realtime-handler auto-wiring harness (FLO-109 / FLO-110, FLO-107 follow-up).

A ``bench update`` rewrites ``apps/frappe/realtime/index.js`` and silently drops
the guarded ``require`` that registers flock_os's ``join`` handler
(``scripts/dev/wire-socketio-handler.sh``). flock_os's ``after_migrate`` /
``after_install`` hooks re-run that script automatically. These tests pin the
guard so a silent regression becomes a red gate instead:

1. The wire script round-trips on a temp bench layout: it inserts the marker,
   a simulated reinstall (restoring the pristine file) drops it, and re-running
   restores it — idempotent + self-healing.
2. ``--check`` is a non-mutating assert (exit 0 wired / 1 absent).
3. The hook locates the bench + script via anchor walk-up (robust to the
   flock_os symlink) and drives the real script end-to-end with only ``frappe``
   stubbed.
4. FLO-110 fail-loud: after driving the script the hook *verifies* the marker
   landed and RAISES :class:`RealtimeWiringError` if it is missing — a dropped
   wiring can never ship silently (the prior best-effort log hid a total failure
   on the live bench for a whole heartbeat).

Runs under plain ``pytest`` (no bench) on any host with ``bash``.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from flock_os.utils import realtime_setup
from flock_os.utils.realtime_setup import WIRING_MARKER, RealtimeWiringError

# The real wiring script, located the same way the hook locates it.
WIRE_SCRIPT = realtime_setup.find_wire_script(realtime_setup.__file__)
assert WIRE_SCRIPT, "wire-socketio-handler.sh not found from module location"

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
	"""A temp bench layout with a pristine frappe realtime index."""
	bench = tmp_path / "bench"
	(bench / "apps" / "frappe" / "realtime").mkdir(parents=True)
	(bench / "sites").mkdir(parents=True)
	(bench / "apps" / "frappe" / "realtime" / "index.js").write_text(PRISTINE_INDEX)
	return bench


def _wire(bench: Path, *flags: str) -> subprocess.CompletedProcess:
	return subprocess.run(
		["bash", str(WIRE_SCRIPT), "--bench", str(bench), *flags],
		capture_output=True,
		text=True,
	)


def _wired(index_path: Path) -> bool:
	return "FLOCK_OS_REALTIME_HANDLER_START" in index_path.read_text()


# --------------------------------------------------------------------------- #
# Anchor-based path resolution (pure, no frappe)
# --------------------------------------------------------------------------- #


def test_find_bench_root_walks_to_apps_and_sites(tmp_path):
	bench = _bench(tmp_path)
	# frappe.__file__ lives at <bench>/apps/frappe/frappe/__init__.py
	frappe_file = bench / "apps" / "frappe" / "frappe" / "__init__.py"
	frappe_file.parent.mkdir(parents=True)
	frappe_file.write_text("")
	assert realtime_setup.find_bench_root(str(frappe_file)) == str(bench)


def test_find_bench_root_returns_none_when_not_a_bench(tmp_path):
	# No apps/ + sites/ siblings anywhere up the tree.
	frappe_file = tmp_path / "deep" / "frappe" / "__init__.py"
	frappe_file.parent.mkdir(parents=True)
	frappe_file.write_text("")
	# tmp_path itself is not a bench; assert we don't accidentally match it.
	result = realtime_setup.find_bench_root(str(frappe_file))
	assert result is None or not os.path.isdir(os.path.join(result, "apps"))


def test_find_wire_script_locates_real_script():
	# The module's own __file__ is under <repo>/flock_os/utils/ — walk-up finds
	# the repo-root scripts/dev/wire-socketio-handler.sh.
	script = realtime_setup.find_wire_script(realtime_setup.__file__)
	assert script and os.path.isfile(script)
	assert script.endswith(os.path.join("scripts", "dev", "wire-socketio-handler.sh"))


# --------------------------------------------------------------------------- #
# Wire-script behaviour (self-healing round-trip + --check)
# --------------------------------------------------------------------------- #


def test_wire_script_round_trip_idempotent_and_self_healing(tmp_path):
	"""The exact regression FLO-109 guards: a reinstall drops the line, re-run restores it."""
	bench = _bench(tmp_path)
	index = bench / "apps" / "frappe" / "realtime" / "index.js"

	# 1. Wire → marker present.
	assert _wire(bench).returncode == 0
	assert _wired(index)

	# 2. Idempotent: wiring again is a no-op (marker still exactly one block).
	first = index.read_text()
	assert _wire(bench).returncode == 0
	assert index.read_text() == first
	assert first.count("FLOCK_OS_REALTIME_HANDLER_START") == 1

	# 3. Simulated `bench update`/reinstall rewrites index.js → marker dropped.
	index.write_text(PRISTINE_INDEX)
	assert not _wired(index), "fixture: reinstall should drop the wiring"

	# 4. Re-wire restores it — the auto-wire hook's job.
	assert _wire(bench).returncode == 0
	assert _wired(index)


def test_check_mode_reflects_wiring_state(tmp_path):
	bench = _bench(tmp_path)
	index = bench / "apps" / "frappe" / "realtime" / "index.js"

	# Absent → --check exits 1.
	assert _wire(bench, "--check").returncode == 1

	# Present → --check exits 0 and makes no change.
	assert _wire(bench).returncode == 0
	wired = index.read_text()
	assert _wire(bench, "--check").returncode == 0
	assert index.read_text() == wired


def test_check_and_revert_are_mutually_exclusive(tmp_path):
	bench = _bench(tmp_path)
	assert _wire(bench, "--check", "--revert").returncode == 2


# --------------------------------------------------------------------------- #
# The hook (frappe stubbed; real script + real module)
# --------------------------------------------------------------------------- #


def _stub_frappe(frappe_file: Path, sink: dict) -> SimpleNamespace:
	"""A frappe stub that records log calls; frappe.__file__ drives find_bench_root."""
	return SimpleNamespace(
		__file__=str(frappe_file),
		logger=lambda name: SimpleNamespace(
			info=lambda *a, **k: sink.setdefault("info", []).append(a),
			warning=lambda *a, **k: sink.setdefault("warning", []).append(a),
		),
	)


def test_rewire_hook_drives_real_script_end_to_end(monkeypatch, tmp_path):
	"""frappe stubbed only; the hook resolves the real bench + real script and wires it."""
	bench = _bench(tmp_path)
	index = bench / "apps" / "frappe" / "realtime" / "index.js"
	frappe_file = bench / "apps" / "frappe" / "frappe" / "__init__.py"
	frappe_file.parent.mkdir(parents=True)
	frappe_file.write_text("")

	monkeypatch.setitem(sys.modules, "frappe", _stub_frappe(frappe_file, {}))

	realtime_setup.rewire_socketio_handler()

	assert _wired(index), "hook should leave the handler wired"


def test_rewire_hook_logs_warning_when_bench_not_found(monkeypatch, tmp_path):
	"""A frappe install with no bench layout must log + swallow, never raise or run."""
	sink: dict = {}
	# frappe.__file__ in a plain tmp tree with no apps/+sites/ ancestors.
	frappe_file = tmp_path / "elsewhere" / "frappe" / "__init__.py"
	frappe_file.parent.mkdir(parents=True)
	frappe_file.write_text("")
	monkeypatch.setitem(sys.modules, "frappe", _stub_frappe(frappe_file, sink))

	realtime_setup.rewire_socketio_handler()  # must not raise

	assert sink.get("warning"), "hook should warn when the bench root is missing"
	assert not sink.get("info")


def test_rewire_hook_short_circuits_when_already_wired(monkeypatch, tmp_path):
	"""An already-wired index must not invoke the script (no-op, no raise)."""
	bench = _bench(tmp_path)
	index = bench / "apps" / "frappe" / "realtime" / "index.js"
	frappe_file = bench / "apps" / "frappe" / "frappe" / "__init__.py"
	frappe_file.parent.mkdir(parents=True)
	frappe_file.write_text("")
	# Pre-wire so the marker is present.
	assert _wire(bench).returncode == 0
	assert _wired(index)

	calls = []
	sink: dict = {}
	monkeypatch.setitem(sys.modules, "frappe", _stub_frappe(frappe_file, sink))

	def _must_not_run(*a, **k):
		calls.append(k)
		raise AssertionError("subprocess.run must not be called when already wired")

	monkeypatch.setattr(realtime_setup.subprocess, "run", _must_not_run)

	# Must not raise and must not invoke the script.
	realtime_setup.rewire_socketio_handler()
	assert calls == [], "already-wired path must short-circuit before subprocess.run"
	assert sink.get("info"), "already-wired path should log info"


def test_rewire_hook_raises_loud_when_wiring_still_missing(monkeypatch, tmp_path):
	"""FLO-110: a wire attempt that leaves the marker absent RAISES, never silent.

	Simulates the exact regression: a ``bench update`` dropped the marker and the
	re-wire failed (subprocess error / Frappe restructured the anchor). The hook
	must fail the migrate loudly with a remediation, not swallow it.
	"""
	bench = _bench(tmp_path)
	frappe_file = bench / "apps" / "frappe" / "frappe" / "__init__.py"
	frappe_file.parent.mkdir(parents=True)
	frappe_file.write_text("")
	sink: dict = {}
	monkeypatch.setitem(sys.modules, "frappe", _stub_frappe(frappe_file, sink))

	def boom(cmd, **kwargs):
		raise FileNotFoundError(str(cmd[1]))

	monkeypatch.setattr(realtime_setup.subprocess, "run", boom)

	with pytest.raises(RealtimeWiringError) as exc_info:
		realtime_setup.rewire_socketio_handler()

	message = str(exc_info.value)
	assert "MISSING" in message
	assert "FLO-107" in message  # names the regression it prevents
	assert "bench restart" in message  # concrete remediation
	assert sink.get("warning"), "the underlying script error should still be logged"


def test_rewire_hook_warns_and_skips_when_index_absent(monkeypatch, tmp_path):
	"""No vendored realtime index -> best-effort warn+return (nothing to wire)."""
	bench = _bench(tmp_path)
	# Remove the realtime index so there is nothing to wire.
	(bench / "apps" / "frappe" / "realtime" / "index.js").unlink()
	frappe_file = bench / "apps" / "frappe" / "frappe" / "__init__.py"
	frappe_file.parent.mkdir(parents=True)
	frappe_file.write_text("")
	sink: dict = {}
	calls = []
	monkeypatch.setitem(sys.modules, "frappe", _stub_frappe(frappe_file, sink))
	monkeypatch.setattr(realtime_setup.subprocess, "run", lambda *a, **k: calls.append(k))

	realtime_setup.rewire_socketio_handler()  # must not raise

	assert sink.get("warning"), "missing index should warn"
	assert calls == [], "must not run the script when there is no index to wire"


def test_python_marker_matches_script_mark_start():
	"""The two sources of truth for "is the handler wired?" must agree.

	``realtime_setup.WIRING_MARKER`` is what the hook verifies;
	``wire-socketio-handler.sh`` inserts/keys on ``MARK_START``. If they drift the
	hook would raise forever (marker never matches) or never detect a drop.
	"""
	script = Path(WIRE_SCRIPT).read_text(encoding="utf-8")
	assert f'MARK_START="{WIRING_MARKER}"' in script
